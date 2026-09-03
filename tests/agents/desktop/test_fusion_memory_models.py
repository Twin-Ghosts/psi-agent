from __future__ import annotations

from _fusion_memory.embedding import cosine_similarity, load_model_config


def test_vector_clients_only_use_dashscope_key() -> None:
    config = load_model_config(
        {
            "DASHSCOPE_API_KEY": "dash-secret",
            "FUSION_MEMORY_EMBEDDING_API_KEY": "wrong-embedding",
            "FUSION_MEMORY_RERANKER_API_KEY": "wrong-rerank",
            "FUSION_MEMORY_MODEL_API_KEY": "llm-secret",
        }
    )
    assert config.embedding.api_key == "dash-secret"
    assert config.embedding.model == "text-embedding-v4"
    assert config.rerank.api_key == "dash-secret"
    assert config.rerank.model == "qwen3-rerank"
    assert config.llm is None


def test_llm_dedicated_key_can_reuse_agent_metadata_but_fallback_is_whole_group() -> None:
    dedicated = load_model_config(
        {
            "FUSION_MEMORY_MODEL_API_KEY": "llm-secret",
            "PSI_AI_PROVIDER": "openai",
            "PSI_AI_MODEL": "qwen-plus",
            "PSI_AI_API_KEY": "agent-secret",
            "PSI_AI_BASE_URL": "https://llm.example/v1",
        }
    )
    assert dedicated.llm is not None
    assert dedicated.llm.api_key == "llm-secret"
    fallback = load_model_config(
        {
            "PSI_AI_PROVIDER": "openai",
            "PSI_AI_MODEL": "qwen-plus",
            "PSI_AI_API_KEY": "agent-secret",
            "PSI_AI_BASE_URL": "https://llm.example/v1",
        }
    )
    assert fallback.llm is not None and fallback.llm.api_key == "agent-secret"
    assert load_model_config({"PSI_AI_MODEL": "qwen-plus", "PSI_AI_API_KEY": "agent-secret"}).llm is None
    no_mixing = load_model_config(
        {
            "FUSION_MEMORY_MODEL_PROVIDER": "deepseek",
            "PSI_AI_PROVIDER": "openai",
            "PSI_AI_MODEL": "agent-model",
            "PSI_AI_API_KEY": "agent-secret",
            "PSI_AI_BASE_URL": "https://agent.example/v1",
        }
    )
    assert no_mixing.llm is not None
    assert (no_mixing.llm.provider, no_mixing.llm.model) == ("openai", "agent-model")


def test_cosine_similarity_is_safe_for_empty_or_mismatched_vectors() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine_similarity([], []) == 0.0
    assert cosine_similarity([1.0], [1.0, 2.0]) == 0.0
