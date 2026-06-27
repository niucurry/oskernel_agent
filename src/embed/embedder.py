"""函数级代码嵌入。

封装为可替换接口 BaseEmbedder（后续可换 UniXcoder 等），默认实现 CodeT5pEmbedder
使用 HuggingFace transformers 加载 Salesforce/codet5p-110m-embedding。

要点：
- 输入用 normalized_code；
- 超过 max_length 的函数滑窗（window_size / window_stride），取各窗口向量均值；
- GPU/CPU 自动检测，按 batch 处理，tqdm 进度条。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np

from .settings import EmbeddingSettings, load_settings


class BaseEmbedder(ABC):
    """嵌入器统一接口。"""

    dim: int

    @abstractmethod
    def encode_batch(self, texts: list[str]) -> np.ndarray:
        """把一批文本编码为 (len(texts), dim) 的 float32 向量。"""


def _windows(ids: list[int], size: int, stride: int) -> list[list[int]]:
    """对 token id 序列滑窗；序列不超过 size 时返回单窗。"""
    if len(ids) <= size:
        return [ids]
    out = []
    start = 0
    while start < len(ids):
        out.append(ids[start : start + size])
        if start + size >= len(ids):
            break
        start += stride
    return out


class CodeT5pEmbedder(BaseEmbedder):
    def __init__(self, settings: EmbeddingSettings | None = None, *, show_progress: bool = True):
        import torch
        from transformers import AutoModel, AutoTokenizer

        self.cfg = settings or load_settings().embedding
        self._torch = torch
        self.show_progress = show_progress

        if self.cfg.device == "auto":
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            self.device = self.cfg.device

        self.tokenizer = AutoTokenizer.from_pretrained(self.cfg.model_name, trust_remote_code=True)
        self.model = AutoModel.from_pretrained(self.cfg.model_name, trust_remote_code=True)
        self.model.to(self.device).eval()
        self.dim = self._probe_dim()

    def _probe_dim(self) -> int:
        v = self._forward_windows([[self.tokenizer.eos_token_id or 1]])
        return int(v.shape[1])

    def _forward_windows(self, windows: list[list[int]]) -> "np.ndarray":
        """对一批等待 pad 的窗口前向，返回 (len(windows), dim)。"""
        torch = self._torch
        pad_id = self.tokenizer.pad_token_id
        if pad_id is None:
            pad_id = self.tokenizer.eos_token_id or 0
        max_len = max(len(w) for w in windows)
        input_ids = torch.full((len(windows), max_len), pad_id, dtype=torch.long)
        attn = torch.zeros((len(windows), max_len), dtype=torch.long)
        for i, w in enumerate(windows):
            input_ids[i, : len(w)] = torch.tensor(w, dtype=torch.long)
            attn[i, : len(w)] = 1
        input_ids = input_ids.to(self.device)
        attn = attn.to(self.device)
        with torch.no_grad():
            out = self.model(input_ids=input_ids, attention_mask=attn)
        if isinstance(out, torch.Tensor):
            emb = out
        elif isinstance(out, (tuple, list)):
            emb = out[0]
        else:  # BaseModelOutput 等
            emb = getattr(out, "last_hidden_state", None)
            if emb is None:
                emb = out[0]
        return emb.detach().cpu().float().numpy()

    def encode_batch(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, getattr(self, "dim", 256)), dtype=np.float32)

        # 1) 并行 tokenization（tokenizer 线程安全，多线程绕过 GIL IO 等待）
        from concurrent.futures import ThreadPoolExecutor

        def _tok(args):
            i, t = args
            ids = self.tokenizer.encode(t or " ", add_special_tokens=True, truncation=False)
            return i, ids or [self.tokenizer.eos_token_id or 1]

        tok_workers = min(8, len(texts))
        with ThreadPoolExecutor(max_workers=tok_workers) as ex:
            tok_results = list(ex.map(_tok, enumerate(texts)))

        all_windows: list[list[int]] = []
        owners: list[int] = []
        for i, ids in tok_results:
            for w in _windows(ids, self.cfg.window_size, self.cfg.window_stride):
                all_windows.append(w)
                owners.append(i)

        # 2) 按 batch 前向所有窗口
        win_vecs = np.zeros((len(all_windows), self.dim), dtype=np.float32)
        rng = range(0, len(all_windows), self.cfg.batch_size)
        if self.show_progress:
            from tqdm import tqdm

            rng = tqdm(rng, desc="embedding", unit="batch")
        for start in rng:
            batch = all_windows[start : start + self.cfg.batch_size]
            win_vecs[start : start + len(batch)] = self._forward_windows(batch)

        # 3) 每个文本取其各窗口向量的均值
        result = np.zeros((len(texts), self.dim), dtype=np.float32)
        owners_arr = np.asarray(owners)
        for i in range(len(texts)):
            mask = owners_arr == i
            result[i] = win_vecs[mask].mean(axis=0)
        return result


def get_embedder(settings: EmbeddingSettings | None = None, *, show_progress: bool = True) -> BaseEmbedder:
    """工厂：按配置返回嵌入器（当前仅 codet5p；预留切换其它模型）。"""
    return CodeT5pEmbedder(settings, show_progress=show_progress)
