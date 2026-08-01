"""
Model engine.

Two backends behind one interface, chosen by looking at the folder you point
Steward at:

  *.safetensors  -> TransformersBackend (Llama 3.2 3B Instruct and friends)
  *.gguf         -> LlamaCppBackend     (Qwen, Mistral, anything quantised)

Swapping models is a folder path, not a code change. Adding a third backend
means adding a class with load() and chat() and one line in pick_backend().
"""
from __future__ import annotations

import gc
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import settings

Progress = Optional[Callable[[str, str], None]]


class EngineError(RuntimeError):
    pass


# ── detection ───────────────────────────────────────────────
def inspect_folder(path: str) -> Dict:
    folder = Path(path).expanduser()
    if not folder.exists():
        raise EngineError(f"That folder does not exist: {folder}")
    if folder.is_file() and folder.suffix.lower() == ".gguf":
        return {"kind": "gguf", "folder": str(folder.parent),
                "file": str(folder), "label": folder.stem, "files": 1}
    if not folder.is_dir():
        raise EngineError(f"Not a folder: {folder}")

    safetensors = sorted(folder.glob("*.safetensors"))
    gguf = sorted(folder.glob("*.gguf"))

    if safetensors:
        has_config = (folder / "config.json").exists()
        if not has_config:
            raise EngineError(
                f"Found {len(safetensors)} safetensors file(s) but no config.json. "
                "Point me at the folder that holds config.json, tokenizer.json "
                "and the weights together."
            )
        return {"kind": "safetensors", "folder": str(folder), "file": None,
                "label": folder.name, "files": len(safetensors)}
    if gguf:
        return {"kind": "gguf", "folder": str(folder), "file": str(gguf[0]),
                "label": gguf[0].stem, "files": len(gguf)}

    raise EngineError(
        f"No model weights in {folder}. I look for *.safetensors or *.gguf."
    )


def pick_backend(kind: str):
    if settings.BACKEND == "transformers":
        return TransformersBackend
    if settings.BACKEND == "llamacpp":
        return LlamaCppBackend
    return TransformersBackend if kind == "safetensors" else LlamaCppBackend


# ── backends ────────────────────────────────────────────────
class Backend:
    name = "base"

    def __init__(self, info: Dict):
        self.info = info
        self.device = "cpu"

    def load(self, progress: Progress = None) -> None:
        raise NotImplementedError

    def chat(self, messages: List[Dict], max_new_tokens: int = None) -> str:
        raise NotImplementedError

    def unload(self) -> None:
        gc.collect()


class TransformersBackend(Backend):
    name = "transformers"

    def __init__(self, info: Dict):
        super().__init__(info)
        self.model = None
        self.tokenizer = None

    def load(self, progress: Progress = None) -> None:
        def say(step, msg):
            if progress:
                progress(step, msg)

        say("step", "Importing torch and transformers")
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:
            raise EngineError(
                f"{exc}. Run install.bat, or pip install torch transformers accelerate"
            ) from exc

        folder = self.info["folder"]
        say("step", "Loading tokenizer")
        self.tokenizer = AutoTokenizer.from_pretrained(folder, local_files_only=True)
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

        use_gpu = settings.PREFER_GPU and torch.cuda.is_available()
        self.device = "cuda" if use_gpu else "cpu"
        if use_gpu:
            vram = torch.cuda.get_device_properties(0).total_memory / 1024**3
            say("step", f"GPU found: {torch.cuda.get_device_name(0)} ({vram:.1f} GB)")
        else:
            say("step", "No CUDA GPU — loading on CPU, this will be slow")

        say("step", f"Loading weights ({self.info['files']} shard(s))")
        kwargs = {
            "local_files_only": True,
            "low_cpu_mem_usage": True,
            "dtype": torch.float16 if use_gpu else torch.float32,
            "device_map": "auto" if use_gpu else "cpu",
        }
        try:
            self.model = AutoModelForCausalLM.from_pretrained(folder, **kwargs)
        except TypeError:
            # older transformers still expect torch_dtype
            kwargs["torch_dtype"] = kwargs.pop("dtype")
            self.model = AutoModelForCausalLM.from_pretrained(folder, **kwargs)

        self.model.eval()
        say("done", f"Ready on {self.device.upper()}")

    def chat(self, messages: List[Dict], max_new_tokens: int = None) -> str:
        import torch

        if not self.model:
            raise EngineError("No model loaded")

        try:
            prompt = self.tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
        except Exception:
            prompt = _plain_prompt(messages)

        inputs = self.tokenizer(
            prompt, return_tensors="pt", truncation=True,
            max_length=settings.CONTEXT_TOKENS,
        )
        inputs = {k: v.to(self.model.device) for k, v in inputs.items()}

        with torch.no_grad():
            out = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens or settings.MAX_NEW_TOKENS,
                temperature=settings.TEMPERATURE,
                top_p=settings.TOP_P,
                do_sample=settings.TEMPERATURE > 0,
                pad_token_id=self.tokenizer.eos_token_id,
            )
        fresh = out[0][inputs["input_ids"].shape[1]:]
        return self.tokenizer.decode(fresh, skip_special_tokens=True).strip()

    def unload(self) -> None:
        self.model = None
        self.tokenizer = None
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass
        super().unload()


class LlamaCppBackend(Backend):
    name = "llamacpp"

    def __init__(self, info: Dict):
        super().__init__(info)
        self.llm = None

    def load(self, progress: Progress = None) -> None:
        def say(step, msg):
            if progress:
                progress(step, msg)

        say("step", "Importing llama-cpp-python")
        try:
            from llama_cpp import Llama
        except ImportError as exc:
            raise EngineError(
                "GGUF support needs llama-cpp-python. "
                "Run: pip install llama-cpp-python"
            ) from exc

        say("step", f"Loading {Path(self.info['file']).name}")
        self.llm = Llama(
            model_path=self.info["file"],
            n_ctx=settings.CONTEXT_TOKENS,
            n_gpu_layers=settings.GPU_LAYERS if settings.PREFER_GPU else 0,
            verbose=False,
        )
        self.device = "gpu" if settings.PREFER_GPU and settings.GPU_LAYERS else "cpu"
        say("done", "Ready")

    def chat(self, messages: List[Dict], max_new_tokens: int = None) -> str:
        if not self.llm:
            raise EngineError("No model loaded")
        result = self.llm.create_chat_completion(
            messages=messages,
            max_tokens=max_new_tokens or settings.MAX_NEW_TOKENS,
            temperature=settings.TEMPERATURE,
            top_p=settings.TOP_P,
        )
        return result["choices"][0]["message"]["content"].strip()

    def unload(self) -> None:
        self.llm = None
        super().unload()


def _plain_prompt(messages: List[Dict]) -> str:
    """Fallback when a model ships without a chat template."""
    parts = []
    for m in messages:
        parts.append(f"### {m['role'].upper()}\n{m['content']}\n")
    parts.append("### ASSISTANT\n")
    return "\n".join(parts)


# ── module-level singleton ──────────────────────────────────
_active: Optional[Backend] = None
_info: Dict = {}


def load(path: str, progress: Progress = None) -> Dict:
    global _active, _info
    info = inspect_folder(path)
    backend_cls = pick_backend(info["kind"])
    if progress:
        progress("step", f"{info['kind']} detected — using {backend_cls.name}")

    if _active:
        _active.unload()
        _active = None

    backend = backend_cls(info)
    backend.load(progress)
    _active = backend
    _info = {**info, "backend": backend.name, "device": backend.device}
    return _info


def chat(messages: List[Dict], max_new_tokens: int = None) -> str:
    if not _active:
        raise EngineError("No model loaded yet")
    return _active.chat(messages, max_new_tokens)


def is_loaded() -> bool:
    return _active is not None


def info() -> Dict:
    return dict(_info)


def unload() -> None:
    global _active, _info
    if _active:
        _active.unload()
    _active = None
    _info = {}
