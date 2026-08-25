import ollama_client


def test_normal_qwen38_is_the_default_code_model():
    assert ollama_client.DEFAULT_CODE_MODEL == "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q2_K_XL"


def test_describe_model_blurbs():
    assert "coding" in ollama_client.describe_model("hf.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:UD-Q4_K_XL")
    assert "coding agent" in ollama_client.describe_model("qwen3.6-agent:27b")
    assert "fast chat" in ollama_client.describe_model("qwen3:14b")
    assert "vision" in ollama_client.describe_model("qwen3-vl:8b")


def test_model_label_includes_parenthetical():
    label = ollama_client.model_label("qwen3:14b")
    assert label.startswith("qwen3:14b (")
    assert label.endswith(")")


def test_strip_ollama_prefix():
    assert ollama_client.strip_ollama_prefix("ollama:qwen3:14b") == "qwen3:14b"
    assert ollama_client.strip_ollama_prefix("qwen3:14b") == "qwen3:14b"


def test_capabilities_includes_installed_models():
    caps = ollama_client.capabilities()
    assert caps["provider"] == "ollama"
    if caps["ready"]:
        assert caps["models"]
        assert all(row.get("label") and "(" in row["label"] for row in caps["models"])
