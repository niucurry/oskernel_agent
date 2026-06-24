"""
Perplexity and log-rank computation — two interchangeable backends.

Backend A: PerplexityScorer (transformers)
  Uses HuggingFace Transformers for model inference.
  Works everywhere; optimised with bfloat16 on CUDA/MPS.

Backend B: VLLMPerplexityCalculator (vLLM)
  Uses vLLM's continuous batching and PagedAttention for 3-5x higher
  throughput on multi-function batches.
  Requires: pip install vllm

Both satisfy the LogRankProvider protocol (detector.py).

Paper formulas:
  Log-p   (eq. 1): (1/t) * sum_i log p(x_i | x_{<i})
  Log-Rank (eq. 3): (1/t) * sum_i log rank(x_i | x_{<i})

Where rank(x_i | x_{<i}) = position of x_i in the vocabulary sorted
by descending probability under the LM at step i.  rank=1 means the
model's top prediction was the actual token.  LLM-generated code tends
to have very low mean log-rank (the model is "confident").

Cache: results are memoized by SHA-256 of the code string.

Usage:
    scorer = PerplexityScorer("sshleifer/tiny-gpt2")
    scorer.load()
    lr = scorer.compute_log_rank("def foo(): return 42")
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Optional

import torch


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _resolve_device(device: str) -> torch.device:
    """Map 'auto' to the best available torch.device."""
    if device == "auto":
        if torch.cuda.is_available():
            return torch.device("cuda")
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return torch.device("mps")
        return torch.device("cpu")
    return torch.device(device)


# ---------------------------------------------------------------------------
# PerplexityScorer — transformers backend
# ---------------------------------------------------------------------------


class PerplexityScorer:
    """Causal-LM scorer using HuggingFace Transformers.

    Args:
        model_id:    HuggingFace model ID or local path.
        device:      'auto', 'cpu', 'cuda', or 'mps'.
        batch_size:  Perturbations scored per forward pass.
        max_length:  Token sequence cap (left-truncated if exceeded).
        cache_dir:   HuggingFace model cache directory.
        score_cache_size: Max entries in the result memo cache.
    """

    def __init__(
        self,
        model_id: str = "codellama/CodeLlama-7b-hf",
        device: str = "auto",
        batch_size: int = 8,
        max_length: int = 2048,
        cache_dir: Optional[Path] = None,
        score_cache_size: int = 10_000,
    ) -> None:
        self.model_id         = model_id
        self._device_str      = device
        self.batch_size       = batch_size
        self.max_length       = max_length
        self.cache_dir        = cache_dir
        self._device: Optional[torch.device] = None
        self._model           = None
        self._tokenizer       = None
        self._score_cache: dict[str, float] = {}   # sha256 → log_rank
        self._cache_size      = score_cache_size

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load model + tokenizer.  Called automatically on first use."""
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self._device = _resolve_device(self._device_str)
        dtype = torch.bfloat16 if self._device.type in ("cuda", "mps") else torch.float32

        cache_str = str(self.cache_dir) if self.cache_dir else None
        self._tokenizer = AutoTokenizer.from_pretrained(
            self.model_id, cache_dir=cache_str, trust_remote_code=True
        )
        if self._tokenizer.pad_token_id is None:
            self._tokenizer.pad_token_id = self._tokenizer.eos_token_id

        self._model = AutoModelForCausalLM.from_pretrained(
            self.model_id,
            dtype=dtype,
            cache_dir=cache_str,
            trust_remote_code=True,
            low_cpu_mem_usage=True,
        ).to(self._device).eval()

    def _ensure_loaded(self) -> None:
        if self._model is None:
            self.load()

    @property
    def device(self) -> torch.device:
        self._ensure_loaded()
        assert self._device is not None
        return self._device

    # ------------------------------------------------------------------
    # Core scoring — public API (LogRankProvider protocol)
    # ------------------------------------------------------------------

    def compute_log_rank(self, code: str) -> float:
        """Mean token log-rank (eq. 3).  Lower → more AI-like."""
        if not code or not code.strip():
            raise ValueError("code must be non-empty")
        h = _sha256(code)
        if h in self._score_cache:
            return self._score_cache[h]
        result = self._log_rank_impl(code)
        if len(self._score_cache) < self._cache_size:
            self._score_cache[h] = result
        return result

    def compute_log_rank_batch(self, codes: list[str]) -> list[float]:
        """Batch compute_log_rank.  Processes in chunks of batch_size."""
        results: list[float] = []
        for i in range(0, len(codes), self.batch_size):
            chunk = codes[i : i + self.batch_size]
            for code in chunk:
                results.append(self.compute_log_rank(code))
        return results

    def compute_token_perplexities(
        self, code: str
    ) -> list[tuple[str, float]]:
        """Return (token_string, log_rank) for every token in *code*.

        Used by the report generator for line-level highlighting.
        """
        if not code or not code.strip():
            return []
        self._ensure_loaded()
        inputs = self._encode(code)
        if inputs["input_ids"].shape[1] < 2:
            return []

        with torch.no_grad():
            outputs = self._model(**inputs)

        logits    = outputs.logits[0]         # (seq, vocab)
        input_ids = inputs["input_ids"][0]    # (seq,)

        token_pairs: list[tuple[str, float]] = []
        for i in range(1, len(input_ids)):
            token_id = input_ids[i].item()
            token_str = self._tokenizer.decode([token_id])
            # Rank of token_id in sorted-descending vocabulary
            rank = int(
                (torch.argsort(logits[i - 1], descending=True) == token_id)
                .nonzero(as_tuple=True)[0]
                .item()
            ) + 1
            token_pairs.append((token_str, math.log(rank)))

        return token_pairs

    # ------------------------------------------------------------------
    # Legacy aliases (paper formula names)
    # ------------------------------------------------------------------

    def log_rank(self, text: str) -> float:
        return self.compute_log_rank(text)

    def log_prob(self, text: str) -> float:
        """Mean per-token log-probability (eq. 1).  Higher → more AI-like."""
        if not text or not text.strip():
            raise ValueError("text must be non-empty")
        self._ensure_loaded()
        inputs = self._encode(text)
        if inputs["input_ids"].shape[1] < 2:
            return 0.0
        with torch.no_grad():
            outputs = self._model(**inputs, labels=inputs["input_ids"])
        return -outputs.loss.item()  # negative NLL = mean log-prob

    def log_prob_batch(self, texts: list[str]) -> list[float]:
        return [self.log_prob(t) for t in texts]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _encode(self, text: str) -> dict[str, torch.Tensor]:
        self._ensure_loaded()
        enc = self._tokenizer(
            text,
            return_tensors="pt",
            truncation=True,
            max_length=self.max_length,
        )
        return {k: v.to(self._device) for k, v in enc.items()}

    @torch.no_grad()
    def _log_rank_impl(self, code: str) -> float:
        """Compute mean log-rank without cache lookup."""
        self._ensure_loaded()
        inputs = self._encode(code)
        seq_len = inputs["input_ids"].shape[1]
        if seq_len < 2:
            return 0.0

        try:
            outputs = self._model(**inputs)
        except RuntimeError as exc:
            if "out of memory" in str(exc).lower():
                torch.cuda.empty_cache()
                # Retry on CPU with reduced precision
                cpu_inputs = {k: v.to("cpu") for k, v in inputs.items()}
                self._model.to("cpu")
                outputs = self._model(**cpu_inputs)
                self._model.to(self._device)
            else:
                raise

        logits    = outputs.logits[0]       # (seq, vocab)
        input_ids = inputs["input_ids"][0]  # (seq,)

        log_ranks: list[float] = []
        for i in range(1, seq_len):
            token_id = input_ids[i].item()
            rank = int(
                (torch.argsort(logits[i - 1], descending=True) == token_id)
                .nonzero(as_tuple=True)[0]
                .item()
            ) + 1
            log_ranks.append(math.log(rank))

        return sum(log_ranks) / len(log_ranks) if log_ranks else 0.0


# ---------------------------------------------------------------------------
# VLLMPerplexityCalculator — vLLM backend
# ---------------------------------------------------------------------------


class VLLMPerplexityCalculator:
    """Log-rank scorer using vLLM's continuous batching engine.

    Achieves 3-5x higher throughput than the transformers backend on
    multi-function batches by exploiting PagedAttention and CUDA graphs.

    Requires:
        pip install vllm

    Args:
        model_id:         HuggingFace model ID or local path.
        top_k_for_rank:   vLLM returns top-K logprobs per position.
                          Tokens outside the top-K are assigned rank K+1.
                          K=50 gives good rank accuracy for code.
        tensor_parallel:  Number of GPUs for tensor parallelism.
        max_length:       Maximum token sequence length.
        cache_dir:        HuggingFace model cache directory.
        score_cache_size: Memo cache entries.
    """

    def __init__(
        self,
        model_id: str = "codellama/CodeLlama-7b-hf",
        top_k_for_rank: int = 50,
        tensor_parallel: int = 1,
        max_length: int = 2048,
        cache_dir: Optional[Path] = None,
        score_cache_size: int = 10_000,
    ) -> None:
        self.model_id       = model_id
        self.top_k          = top_k_for_rank
        self.tensor_parallel = tensor_parallel
        self.max_length     = max_length
        self.cache_dir      = cache_dir
        self._llm           = None
        self._tokenizer     = None
        self._score_cache: dict[str, float] = {}
        self._cache_size    = score_cache_size

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Initialise the vLLM engine.  Called automatically on first use."""
        try:
            from vllm import LLM
            from transformers import AutoTokenizer
        except ImportError as exc:
            raise ImportError(
                "vllm is not installed.  Install with:\n"
                "  pip install vllm\n"
                "Or use the transformers backend (default)."
            ) from exc

        cache_dir_str = str(self.cache_dir) if self.cache_dir else None
        self._tokenizer = AutoTokenizer.from_pretrained(
            self.model_id,
            cache_dir=cache_dir_str,
            trust_remote_code=True,
        )
        self._llm = LLM(
            model=self.model_id,
            download_dir=cache_dir_str,
            tensor_parallel_size=self.tensor_parallel,
            max_model_len=self.max_length,
            trust_remote_code=True,
        )

    def _ensure_loaded(self) -> None:
        if self._llm is None:
            self.load()

    # ------------------------------------------------------------------
    # LogRankProvider protocol
    # ------------------------------------------------------------------

    def compute_log_rank(self, code: str) -> float:
        return self.compute_log_rank_batch([code])[0]

    def compute_log_rank_batch(self, codes: list[str]) -> list[float]:
        """Batch log-rank via vLLM prompt_logprobs.

        All *codes* are submitted to vLLM in a single call, allowing the
        engine to use continuous batching for maximum GPU utilisation.
        """
        if not codes:
            return []

        self._ensure_loaded()

        # Separate cached from uncached
        hashes   = [_sha256(c) for c in codes]
        uncached_idx   = [i for i, h in enumerate(hashes) if h not in self._score_cache]
        uncached_codes = [codes[i] for i in uncached_idx]

        if uncached_codes:
            scores = self._score_vllm(uncached_codes)
            for idx, score in zip(uncached_idx, scores):
                h = hashes[idx]
                if len(self._score_cache) < self._cache_size:
                    self._score_cache[h] = score

        return [self._score_cache[h] for h in hashes]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _score_vllm(self, codes: list[str]) -> list[float]:
        try:
            from vllm import SamplingParams as _SP
        except ImportError:
            # Allow test mocks to inject _llm without installing vllm.
            class _SP:  # type: ignore[no-redef]
                def __init__(self, **kw): pass

        params = _SP(
            max_tokens=1,
            prompt_logprobs=self.top_k,
            temperature=0.0,
        )
        outputs = self._llm.generate(codes, params)

        results: list[float] = []
        for output in outputs:
            log_ranks: list[float] = []
            token_ids    = output.prompt_token_ids
            prompt_lprobs = output.prompt_logprobs  # List[Optional[Dict[int, Logprob]]]

            if not prompt_lprobs:
                results.append(0.0)
                continue

            # Skip position 0 (first token has no prefix)
            for pos, (tok_id, pos_lp) in enumerate(
                zip(token_ids[1:], prompt_lprobs[1:]), start=1
            ):
                if pos_lp is None:
                    continue
                # vLLM ≥ 0.4: Logprob has .rank attribute
                if tok_id in pos_lp:
                    lp_obj = pos_lp[tok_id]
                    rank = getattr(lp_obj, "rank", None)
                    if rank is None:
                        # Fall back: position in the returned sorted dict
                        rank = (
                            sorted(pos_lp, key=lambda k: pos_lp[k].logprob, reverse=True)
                            .index(tok_id)
                            + 1
                        )
                else:
                    # Token not in top-K → rank > K
                    rank = self.top_k + 1

                log_ranks.append(math.log(max(1, rank)))

            results.append(sum(log_ranks) / len(log_ranks) if log_ranks else 0.0)

        return results

    def compute_token_perplexities(
        self, code: str
    ) -> list[tuple[str, float]]:
        """Token-level log-ranks for line highlighting."""
        self._ensure_loaded()
        from vllm import SamplingParams

        params = SamplingParams(max_tokens=1, prompt_logprobs=self.top_k)
        outputs = self._llm.generate([code], params)
        output  = outputs[0]

        if not output.prompt_logprobs:
            return []

        token_ids    = output.prompt_token_ids
        prompt_lprobs = output.prompt_logprobs
        pairs: list[tuple[str, float]] = []

        for tok_id, pos_lp in zip(token_ids[1:], prompt_lprobs[1:]):
            if pos_lp is None:
                continue
            token_str = self._tokenizer.decode([tok_id])
            rank = self.top_k + 1
            if tok_id in pos_lp:
                lp_obj = pos_lp[tok_id]
                rank = getattr(lp_obj, "rank", rank)
            pairs.append((token_str, math.log(max(1, rank))))

        return pairs


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_calculator(
    engine: str = "transformers",
    model_id: str = "codellama/CodeLlama-7b-hf",
    device: str = "auto",
    batch_size: int = 8,
    max_length: int = 2048,
    cache_dir: Optional[Path] = None,
    top_k_for_rank: int = 50,
    tensor_parallel: int = 1,
):
    """Instantiate and load the appropriate calculator backend.

    Args:
        engine: 'transformers' (default) or 'vllm'.
    """
    if engine == "vllm":
        calc = VLLMPerplexityCalculator(
            model_id=model_id,
            top_k_for_rank=top_k_for_rank,
            tensor_parallel=tensor_parallel,
            max_length=max_length,
            cache_dir=cache_dir,
        )
    else:
        calc = PerplexityScorer(
            model_id=model_id,
            device=device,
            batch_size=batch_size,
            max_length=max_length,
            cache_dir=cache_dir,
        )
    calc.load()
    return calc
