"""
aicd — AI-generated code detector CLI.

Commands:
  scan       Scan a repository for LLM-generated code.
  calibrate  Calibrate thresholds from labelled sample directories.
  inspect    Analyse a single function in a source file.
  version    Show version and model information.

Configuration priority (highest to lowest):
  1. CLI flags
  2. ./aicd.yaml
  3. ~/.config/aicd/config.yaml
  4. AICD_* environment variables
  5. Built-in defaults
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import signal
import statistics
import sys
from pathlib import Path
from typing import Annotated, List, Optional

import typer
import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import __version__
from .aggregator import RepoAggregator
from .detector import DetectCodeGPT, LogRankProvider
from .extractor import FunctionExtractor
from .models import FunctionBlock, Language
from .pipeline import DetectionPipeline, ThresholdConfig
from .report import ReportGenerator

# ---------------------------------------------------------------------------
# App + console
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="aicd",
    help="AI-generated code detector for Git repositories (DetectCodeGPT method).",
    add_completion=False,
    rich_markup_mode="rich",
    no_args_is_help=True,
)
console = Console()

# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------

MODEL_IDS: dict[str, str] = {
    "codellama-7b":  "codellama/CodeLlama-7b-hf",
    "codellama-13b": "codellama/CodeLlama-13b-hf",
    "starcoder-3b":  "bigcode/starcoder2-3b",
}

# ---------------------------------------------------------------------------
# Calculator factory — replace this in tests via monkeypatch
# ---------------------------------------------------------------------------

def _make_calculator(
    model_id: str,
    device: str,
    cache_dir: Path,
    engine: str = "transformers",
) -> LogRankProvider:
    """Instantiate and load the appropriate calculator backend.

    Separated from scan() so tests can monkeypatch this reference.

    Args:
        engine: 'transformers' (default) or 'vllm'.
    """
    from .perplexity import create_calculator
    return create_calculator(
        engine=engine,
        model_id=model_id,
        device=device,
        cache_dir=cache_dir,
    )


_calculator_factory = _make_calculator   # mutable sentinel

# ---------------------------------------------------------------------------
# pydantic-settings: env-var + default config
# ---------------------------------------------------------------------------

class AicdSettings(BaseSettings):
    """Base configuration loaded from environment variables (AICD_* prefix)."""

    model_config = SettingsConfigDict(env_prefix="AICD_", extra="ignore")

    model: str = "codellama-7b"
    device: str = "auto"
    threshold_log_rank_low: float = 1.5
    threshold_log_rank_high: float = 3.0
    threshold_detect_score: float = 0.1
    k_perturbations: int = 50
    batch_size: int = 8
    git_blame: bool = True
    cache_dir: Path = Field(default_factory=lambda: Path.home() / ".cache" / "aicd")
    min_confidence_llm: float = 0.7
    top_n_files: int = 20


def _load_yaml_config(path: Path) -> dict:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _merged_settings(**cli_overrides) -> AicdSettings:
    """Merge env defaults < global YAML < local YAML < CLI flags."""
    base = AicdSettings().model_dump()

    global_cfg = Path.home() / ".config" / "aicd" / "config.yaml"
    if global_cfg.exists():
        base.update(_load_yaml_config(global_cfg))

    local_cfg = Path("aicd.yaml")
    if local_cfg.exists():
        base.update(_load_yaml_config(local_cfg))

    # Apply CLI overrides (only non-None values)
    for k, v in cli_overrides.items():
        if v is not None:
            base[k] = v

    return AicdSettings.model_validate(base)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_key(repo_path: Path, cfg: AicdSettings) -> str:
    payload = str(repo_path.resolve()) + json.dumps(cfg.model_dump(), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


def _cache_path(cfg: AicdSettings, repo_path: Path) -> Path:
    key = _cache_key(repo_path, cfg)
    return cfg.cache_dir / f"scan_{key}.jsonl"


def _save_cache(path: Path, results: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(r.model_dump_json() for r in results),
        encoding="utf-8",
    )


def _load_cache(path: Path) -> list:
    from .pipeline import PipelineResult
    if not path.exists():
        return []
    results = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                results.append(PipelineResult.model_validate_json(line))
            except Exception:
                pass
    return results


# ---------------------------------------------------------------------------
# Interrupt-safe scan state
# ---------------------------------------------------------------------------

_partial_results: list = []
_interrupt_cache: Path | None = None


def _register_interrupt(cache_path: Path) -> None:
    global _interrupt_cache
    _interrupt_cache = cache_path

    def _handler(sig, frame):
        if _partial_results and _interrupt_cache:
            _save_cache(_interrupt_cache, _partial_results)
            console.print(f"\n[yellow]Interrupted. Progress saved to {_interrupt_cache}")
            console.print("[yellow]Resume with: [bold]aicd scan <repo> --resume[/bold]")
        raise SystemExit(130)

    # Skip signal registration in test environments (CliRunner can't handle SIGINT)
    if not os.environ.get("AICD_NO_SIGNAL"):
        signal.signal(signal.SIGINT, _handler)


# ---------------------------------------------------------------------------
# Helpers: language parsing, pattern filtering
# ---------------------------------------------------------------------------

_LANG_MAP = {lang.value: lang for lang in Language}


def _parse_languages(raw: str | None) -> list[Language] | None:
    if not raw:
        return None
    result = []
    for part in raw.split(","):
        part = part.strip().lower()
        if part not in _LANG_MAP:
            console.print(f"[red]Unknown language: {part!r}. Valid: {list(_LANG_MAP)}")
            raise typer.Exit(1)
        result.append(_LANG_MAP[part])
    return result or None


def _matches_any(path: Path, patterns: list[str]) -> bool:
    s = path.as_posix()
    return any(fnmatch.fnmatch(s, p) or fnmatch.fnmatch(path.name, p) for p in patterns)


def _filter_blocks(
    blocks: list[FunctionBlock],
    include: list[str],
    exclude: list[str],
) -> list[FunctionBlock]:
    if not include and not exclude:
        return blocks
    result = []
    for b in blocks:
        if exclude and _matches_any(b.file_path, exclude):
            continue
        if include and not _matches_any(b.file_path, include):
            continue
        result.append(b)
    return result


# ---------------------------------------------------------------------------
# Rich helpers
# ---------------------------------------------------------------------------

def _colour_class(rate: float) -> str:
    if rate >= 0.5:
        return "red"
    if rate >= 0.2:
        return "yellow"
    return "green"


def _print_summary(report, output_dir: Path) -> None:
    o = report.overall
    colour = _colour_class(o.llm_ratio_by_count)

    stats_table = Table.grid(padding=(0, 2))
    stats_table.add_column(style="bold")
    stats_table.add_column()
    rows = [
        ("Total functions",   str(o.total_functions)),
        ("LLM suspected",     f"[{colour}]{o.llm_count}[/{colour}] ({o.llm_ratio_by_count:.1%})"),
        ("Human",             str(o.human_count)),
        ("Uncertain/skipped", str(o.uncertain_count)),
        ("Avg confidence",    f"{o.average_confidence:.2f}"),
        ("Files scanned",     str(report.total_files_scanned)),
    ]
    for k, v in rows:
        stats_table.add_row(k, v)

    report_html = output_dir / "report.html"
    console.print(Panel(
        stats_table,
        title="[bold]Scan Complete[/bold]",
        subtitle=f"[dim]{report.repo_path}[/dim]",
        border_style=colour,
        expand=False,
    ))

    if o.llm_count > 0:
        console.print(f"\n[bold green]Report written to:[/bold green] {output_dir}")
        console.print(f"  Open in browser: [bold cyan]open {report_html}[/bold cyan]")
    else:
        console.print("[green]No LLM-generated code detected.[/green]")


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

@app.command()
def scan(
    repo_path: Annotated[Path, typer.Argument(help="Path to the Git repository to scan.")],
    output: Annotated[Path, typer.Option("--output", "-o", help="Output directory for reports.")] = Path("./aicd-report"),
    model: Annotated[Optional[str], typer.Option("--model", "-m", help="Model: codellama-7b | starcoder-3b | codellama-13b")] = None,
    languages: Annotated[Optional[str], typer.Option("--languages", "-l", help="Comma-separated languages, e.g. python,go")] = None,
    include_pattern: Annotated[Optional[List[str]], typer.Option("--include-pattern", help="Glob pattern for files to include (repeatable).")] = None,
    exclude_pattern: Annotated[Optional[List[str]], typer.Option("--exclude-pattern", help="Glob pattern for files to exclude (repeatable).")] = None,
    threshold_lr_low: Annotated[Optional[float], typer.Option("--threshold-log-rank-low")] = None,
    threshold_lr_high: Annotated[Optional[float], typer.Option("--threshold-log-rank-high")] = None,
    threshold_detect: Annotated[Optional[float], typer.Option("--threshold-detect-score")] = None,
    k_perturbations: Annotated[Optional[int], typer.Option("--k-perturbations")] = None,
    batch_size: Annotated[Optional[int], typer.Option("--batch-size")] = None,
    engine: Annotated[str, typer.Option("--engine", "-e",
        help="Inference backend: transformers (default) | vllm"
    )] = "transformers",
    git_blame: Annotated[bool, typer.Option("--git-blame/--no-git-blame")] = True,
    cache_dir: Annotated[Optional[Path], typer.Option("--cache-dir")] = None,
    resume: Annotated[bool, typer.Option("--resume/--no-resume", help="Resume from cached intermediate results.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Scan a Git repository for LLM-generated code."""
    # ---- Validate repo ----
    if not repo_path.exists():
        console.print(f"[red]Error: repository path not found: {repo_path}[/red]")
        raise typer.Exit(1)

    if engine not in ("transformers", "vllm"):
        console.print(f"[red]Unknown engine {engine!r}. Choose: transformers | vllm[/red]")
        raise typer.Exit(1)

    cfg = _merged_settings(
        model=model,
        threshold_log_rank_low=threshold_lr_low,
        threshold_log_rank_high=threshold_lr_high,
        threshold_detect_score=threshold_detect,
        k_perturbations=k_perturbations,
        batch_size=batch_size,
        git_blame=git_blame,
        cache_dir=cache_dir,
    )

    model_id = MODEL_IDS.get(cfg.model, cfg.model)
    lang_filter = _parse_languages(languages)

    if verbose:
        console.print(f"[dim]Model:      {model_id}")
        console.print(f"[dim]Thresholds: LR<{cfg.threshold_log_rank_low} LLM, LR>{cfg.threshold_log_rank_high} Human, NPR>{cfg.threshold_detect_score}")
        console.print(f"[dim]k-perturbs: {cfg.k_perturbations}, batch: {cfg.batch_size}")

    # ---- Cache / resume ----
    c_path = _cache_path(cfg, repo_path)
    cached_results = _load_cache(c_path) if resume else []
    cached_keys = {
        (str(r.function_block.file_path), r.function_block.name)
        for r in cached_results
    }
    _register_interrupt(c_path)

    # ---- Extract functions ----
    console.print(f"\n[bold]Scanning[/bold] {repo_path.resolve()} ...")
    extractor = FunctionExtractor(languages=lang_filter)

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as prog:
        extract_task = prog.add_task("[cyan]Extracting functions", total=None)
        all_blocks = list(extractor.extract_from_repo(repo_path))
        prog.update(extract_task, total=len(all_blocks), completed=len(all_blocks))

    all_blocks = _filter_blocks(
        all_blocks,
        include=list(include_pattern or []),
        exclude=list(exclude_pattern or []),
    )

    # Remove already-cached
    new_blocks = [
        b for b in all_blocks
        if (str(b.file_path), b.name) not in cached_keys
    ]

    console.print(f"  Found [bold]{len(all_blocks)}[/bold] functions"
                  f" ({len(new_blocks)} to analyse, {len(cached_results)} cached)")

    if not all_blocks:
        console.print("[yellow]No functions found. Check --languages or repository contents.")
        raise typer.Exit(0)

    # ---- Load model ----
    if new_blocks:
        engine_label = f"[cyan]{engine}[/cyan]"
        console.print(f"\n[bold]Loading model[/bold] {model_id} ({engine_label}) ...")
        if engine == "vllm":
            try:
                import vllm  # noqa: F401
            except ImportError:
                console.print("[red]vllm is not installed.[/red]")
                console.print("  Install with: [bold]pip install vllm[/bold]")
                console.print("  Or use the default engine: [bold]--engine transformers[/bold]")
                raise typer.Exit(1)
        try:
            calculator = _calculator_factory(
                model_id=model_id,
                device=cfg.device,
                cache_dir=cfg.cache_dir,
                engine=engine,
            )
        except Exception as exc:
            _handle_model_error(exc, cfg)
            raise typer.Exit(2)

        # ---- Detection pipeline ----
        thresh_cfg = ThresholdConfig(
            log_rank_llm_threshold=cfg.threshold_log_rank_low,
            log_rank_human_threshold=cfg.threshold_log_rank_high,
            detect_score_threshold=cfg.threshold_detect_score,
        )
        detector = DetectCodeGPT(
            provider=calculator,
            k=cfg.k_perturbations,
            threshold=cfg.threshold_detect_score,
            batch_size=cfg.batch_size,
        )
        pipeline = DetectionPipeline(calculator, detector, cfg=thresh_cfg)

        new_results = pipeline.process_batch(new_blocks, show_progress=True)
        _partial_results.extend(new_results)
        _save_cache(c_path, new_results)

        all_results = cached_results + new_results
    else:
        all_results = cached_results

    # ---- Aggregate ----
    agg = RepoAggregator(
        enable_git_blame=git_blame,
        top_n_files=cfg.top_n_files,
        min_confidence_llm=cfg.min_confidence_llm,
    )
    report = agg.aggregate(all_results, repo_path)

    # ---- Generate report ----
    output.mkdir(parents=True, exist_ok=True)
    gen = ReportGenerator()
    gen.generate(report, output)

    _print_summary(report, output)


def _handle_model_error(exc: Exception, cfg: AicdSettings) -> None:
    msg = str(exc)
    if "connection" in msg.lower() or "download" in msg.lower() or "http" in msg.lower():
        console.print("[red]Model download failed.[/red]")
        console.print("  Check network connection.")
        console.print("  To use a mirror: [bold]export HF_ENDPOINT=https://hf-mirror.com[/bold]")
    elif "out of memory" in msg.lower() or "oom" in msg.lower() or "cuda" in msg.lower():
        console.print("[red]GPU out of memory.[/red]")
        console.print(f"  Try: [bold]aicd scan ... --batch-size 1[/bold]")
        console.print(f"  Or use a smaller model: [bold]aicd scan ... --model starcoder-3b[/bold]")
    else:
        console.print(f"[red]Model loading failed:[/red] {exc}")


# ---------------------------------------------------------------------------
# calibrate
# ---------------------------------------------------------------------------

@app.command()
def calibrate(
    samples_dir: Annotated[Path, typer.Argument(
        help="Directory with llm/ and human/ subdirectories."
    )],
    output_config: Annotated[Path, typer.Option("--output", "-o",
        help="Path for the YAML thresholds file."
    )] = Path.home() / ".config" / "aicd" / "thresholds.yaml",
    report_output: Annotated[Optional[Path], typer.Option("--report",
        help="Path for the HTML calibration report (default: <output_config_dir>/calibration.html)."
    )] = None,
    model: Annotated[Optional[str], typer.Option("--model", "-m")] = None,
    languages: Annotated[Optional[str], typer.Option("--languages", "-l",
        help="Comma-separated languages to calibrate."
    )] = None,
    max_per_class: Annotated[int, typer.Option("--max-per-class",
        help="Max functions per (language, class) to limit runtime."
    )] = 200,
    compute_detect_score: Annotated[bool, typer.Option("--compute-detect-score/--no-detect-score",
        help="Also calibrate the NPR detect_score (slow: requires DetectCodeGPT)."
    )] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Calibrate detection thresholds from labelled code samples.

    Expects [blue]samples_dir/llm/[/blue] and [blue]samples_dir/human/[/blue] subdirectories.

    Per-language ROC curves are computed using Youden's J criterion.
    Results are saved to a YAML file and an interactive HTML report.

    Example directory layout:

      samples/
      |-- llm/
      |   |-- python/    (*.py files)
      |   `-- java/      (*.java files)
      `-- human/
          |-- python/
          `-- java/
    """
    from .calibrator import Calibrator

    # ---- Validate input ----
    llm_dir   = samples_dir / "llm"
    human_dir = samples_dir / "human"
    if not llm_dir.exists() or not human_dir.exists():
        console.print(f"[red]Expected subdirectories:[/red]  {llm_dir}  and  {human_dir}")
        raise typer.Exit(1)

    cfg = _merged_settings(model=model)
    model_id = MODEL_IDS.get(cfg.model, cfg.model)
    lang_filter = _parse_languages(languages)

    console.print(f"\n[bold]Calibrating[/bold] with model [cyan]{model_id}[/cyan] ...")
    console.print(f"  Samples: {samples_dir.resolve()}")

    # ---- Load model ----
    try:
        calculator = _calculator_factory(model_id=model_id, device=cfg.device, cache_dir=cfg.cache_dir)
    except Exception as exc:
        _handle_model_error(exc, cfg)
        raise typer.Exit(2)

    # ---- Build detector for optional detect_score calibration ----
    detector = None
    if compute_detect_score:
        detector = DetectCodeGPT(
            provider=calculator,
            k=cfg.k_perturbations,
            threshold=cfg.threshold_detect_score,
            batch_size=cfg.batch_size,
        )

    # ---- Run calibration ----
    calibrator = Calibrator(
        provider=calculator,
        detector=detector,
        max_per_class=max_per_class,
        compute_detect_score=compute_detect_score,
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        console=console,
    ) as prog:
        task = prog.add_task("[cyan]Running calibration ...", total=None)
        try:
            report = calibrator.run(
                samples_dir=samples_dir,
                model_id=model_id,
                languages=[l.value for l in lang_filter] if lang_filter else None,
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
        prog.update(task, completed=1, total=1)

    if not report.results:
        console.print("[red]No functions extracted. Check samples_dir structure.[/red]")
        raise typer.Exit(1)

    # ---- Save thresholds ----
    calibrator.save_thresholds(report, output_config)

    # ---- Generate HTML report ----
    html_out = report_output or output_config.parent / "calibration.html"
    try:
        calibrator.render_html(report, html_out)
        html_generated = True
    except Exception as exc:
        console.print(f"[yellow]HTML report skipped:[/yellow] {exc}")
        html_generated = False

    # ---- Print summary table ----
    t = Table(title="Calibrated Thresholds", box=None, show_header=True, border_style="dim")
    t.add_column("Language", style="bold")
    t.add_column("N LLM", justify="right")
    t.add_column("N Human", justify="right")
    t.add_column("LR AUC", style="cyan")
    t.add_column("LR Low", style="red")
    t.add_column("LR High", style="green")
    t.add_column("DS Thresh", style="blue")

    for r in report.results:
        auc_str = f"{r.lr_roc.auc:.4f}" if r.lr_roc else "N/A"
        t.add_row(
            r.language, str(r.n_llm), str(r.n_human),
            auc_str,
            str(r.recommended_lr_low),
            str(r.recommended_lr_high),
            str(r.recommended_ds),
        )

    console.print(t)
    console.print(f"\n[bold green]Thresholds saved to:[/bold green] {output_config}")
    if html_generated:
        console.print(f"[bold green]Calibration report:[/bold green]  {html_out}")
        console.print(f"  Open: [bold cyan]open {html_out}[/bold cyan]")

    # ---- Usage hint ----
    if report.results:
        r0 = report.results[0]
        console.print(Panel(
            f"Apply with:\n"
            f"[bold]aicd scan <repo>[/bold]\\\n"
            f"  --threshold-log-rank-low  {r0.recommended_lr_low}\\\n"
            f"  --threshold-log-rank-high {r0.recommended_lr_high}\\\n"
            f"  --threshold-detect-score  {r0.recommended_ds}",
            title="[bold]Next Steps[/bold]",
            border_style="blue",
            expand=False,
        ))


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------

@app.command()
def inspect(
    file_path: Annotated[Path, typer.Argument(help="Source file containing the function.")],
    function_name: Annotated[str, typer.Argument(help="Function name to inspect.")],
    model: Annotated[Optional[str], typer.Option("--model", "-m")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Analyse a single function in detail."""
    if not file_path.exists():
        console.print(f"[red]File not found: {file_path}[/red]")
        raise typer.Exit(1)

    extractor = FunctionExtractor()
    blocks = extractor.extract_from_file(file_path)
    target = next((b for b in blocks if b.name == function_name), None)

    if target is None:
        names = [b.name for b in blocks]
        console.print(f"[red]Function {function_name!r} not found.[/red]")
        console.print(f"  Available: {names}")
        raise typer.Exit(1)

    console.print(f"\n[bold]Function:[/bold] {target.qualified_name or target.name}")
    console.print(f"[bold]File:[/bold]     {file_path}:{target.start_line}-{target.end_line}")
    console.print(f"[bold]Language:[/bold] {target.language.value}  LOC: {target.loc}")

    cfg = _merged_settings(model=model)
    model_id = MODEL_IDS.get(cfg.model, cfg.model)

    try:
        calculator = _calculator_factory(model_id=model_id, device=cfg.device, cache_dir=cfg.cache_dir)
    except Exception as exc:
        _handle_model_error(exc, cfg)
        raise typer.Exit(2)

    thresh_cfg = ThresholdConfig(
        log_rank_llm_threshold=cfg.threshold_log_rank_low,
        log_rank_human_threshold=cfg.threshold_log_rank_high,
        detect_score_threshold=cfg.threshold_detect_score,
    )
    detector  = DetectCodeGPT(provider=calculator, k=cfg.k_perturbations)
    pipeline  = DetectionPipeline(calculator, detector, cfg=thresh_cfg)
    result    = pipeline.process_function(target)

    colour = "red" if result.label == "LLM" else "green" if result.label == "Human" else "yellow"
    console.print(f"\n[bold]Verdict:[/bold] [{colour}]{result.label}[/{colour}]  confidence={result.confidence:.2f}  stage={result.stage}")

    if result.log_rank is not None:
        console.print(f"  Log-rank:   {result.log_rank:.4f}")
    if result.detect_score is not None:
        console.print(f"  NPR score:  {result.detect_score:.4f}")

    console.print("\n[bold]Reasoning:[/bold]")
    for reason in result.reasons:
        console.print(f"  [dim]-[/dim] {reason}")

    if verbose:
        console.print("\n[bold]Source:[/bold]")
        console.print(target.source)


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------

@app.command()
def version(
    model: Annotated[Optional[str], typer.Option("--model", "-m")] = None,
) -> None:
    """Show version and optional model information."""
    console.print(Panel(
        f"[bold]ai-code-detector[/bold]  v{__version__}\n"
        f"Method: DetectCodeGPT (ACM TOSEM 2026)\n"
        f"Supported languages: Python, Java, Go, C, C++, JavaScript, TypeScript",
        title="Version",
        expand=False,
    ))

    console.print("\n[bold]Available models:[/bold]")
    for short, full in MODEL_IDS.items():
        console.print(f"  [cyan]{short:<16}[/cyan]  {full}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app()


if __name__ == "__main__":
    main()
