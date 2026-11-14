"""Build a tiny random-weight llama GGUF so the real llama.cpp path can be tested without downloads."""
from pathlib import Path

import numpy as np
from gguf import GGUFWriter


def make(path: Path, n_embd=32, n_layer=2, n_head=4, n_ff=64, n_ctx=256) -> Path:
    toks = ["<unk>", "<s>", "</s>"] + [f"<0x{i:02X}>" for i in range(256)] + ["▁"] + [chr(c) for c in range(33, 127)] + \
           ["▁the", "▁a", "▁hello", "▁world"]
    types = [2, 3, 3] + [6] * 256 + [1] * (len(toks) - 259)
    n_vocab = len(toks)
    w = GGUFWriter(str(path), "llama")
    w.add_name("tiny-test")
    w.add_context_length(n_ctx)
    w.add_embedding_length(n_embd)
    w.add_block_count(n_layer)
    w.add_feed_forward_length(n_ff)
    w.add_head_count(n_head)
    w.add_head_count_kv(n_head)
    w.add_layer_norm_rms_eps(1e-5)
    w.add_rope_dimension_count(n_embd // n_head)
    w.add_file_type(0)
    w.add_tokenizer_model("llama")
    w.add_token_list(toks)
    w.add_token_scores([0.0] * n_vocab)
    w.add_token_types(types)
    w.add_bos_token_id(1)
    w.add_eos_token_id(2)
    w.add_unk_token_id(0)
    w.add_chat_template("{% for m in messages %}{{ m['role'] }}: {{ m['content'] }}\n{% endfor %}assistant:")
    rng = np.random.default_rng(0)
    t = lambda *s: rng.normal(0, 0.05, s).astype(np.float32)
    w.add_tensor("token_embd.weight", t(n_vocab, n_embd))
    w.add_tensor("output_norm.weight", np.ones(n_embd, np.float32))
    w.add_tensor("output.weight", t(n_vocab, n_embd))
    for i in range(n_layer):
        p = f"blk.{i}."
        w.add_tensor(p + "attn_norm.weight", np.ones(n_embd, np.float32))
        for n in ("attn_q", "attn_k", "attn_v", "attn_output"):
            w.add_tensor(p + n + ".weight", t(n_embd, n_embd))
        w.add_tensor(p + "ffn_norm.weight", np.ones(n_embd, np.float32))
        w.add_tensor(p + "ffn_gate.weight", t(n_ff, n_embd))
        w.add_tensor(p + "ffn_up.weight", t(n_ff, n_embd))
        w.add_tensor(p + "ffn_down.weight", t(n_embd, n_ff))
    w.write_header_to_file()
    w.write_kv_data_to_file()
    w.write_tensors_to_file()
    w.close()
    return path
