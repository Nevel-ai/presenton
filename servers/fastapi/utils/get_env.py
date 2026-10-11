import os


def get_can_change_keys_env():
    return os.getenv("CAN_CHANGE_KEYS")


def get_database_url_env():
    return os.getenv("DATABASE_URL")


def get_app_data_directory_env():
    return os.getenv("APP_DATA_DIRECTORY")


def get_temp_directory_env():
    return os.getenv("TEMP_DIRECTORY")


def get_user_config_path_env():
    return os.getenv("USER_CONFIG_PATH")


def get_llm_provider_env():
    return os.getenv("LLM")


def get_anthropic_api_key_env():
    return os.getenv("ANTHROPIC_API_KEY")


def get_anthropic_model_env():
    return os.getenv("ANTHROPIC_MODEL")


def get_ollama_url_env():
    return os.getenv("OLLAMA_URL")


def get_custom_llm_url_env():
    return os.getenv("CUSTOM_LLM_URL")


def get_openai_api_key_env():
    return os.getenv("OPENAI_API_KEY")


def get_openai_proxy_url_env():
    # Full proxy URL, can include auth, e.g. http://user:pass@host:3128
    return os.getenv("OPENAI_PROXY_URL") or None


def get_openai_model_env():
    return os.getenv("OPENAI_MODEL")


def get_google_api_key_env():
    return os.getenv("GOOGLE_API_KEY")


def get_google_model_env():
    return os.getenv("GOOGLE_MODEL")


def get_custom_llm_api_key_env():
    return os.getenv("CUSTOM_LLM_API_KEY")


def get_ollama_model_env():
    return os.getenv("OLLAMA_MODEL")


def get_custom_model_env():
    return os.getenv("CUSTOM_MODEL")


def get_pexels_api_key_env():
    return os.getenv("PEXELS_API_KEY")


def get_image_provider_env():
    return os.getenv("IMAGE_PROVIDER")


def get_pixabay_api_key_env():
    return os.getenv("PIXABAY_API_KEY")


def get_tool_calls_env():
    return os.getenv("TOOL_CALLS")


def get_disable_thinking_env():
    return os.getenv("DISABLE_THINKING")


def get_extended_reasoning_env():
    return os.getenv("EXTENDED_REASONING")


def get_web_grounding_env():
    return os.getenv("WEB_GROUNDING")


def get_ideogram_api_key_env():
    return os.getenv("IDEOGRAM_API_KEY")


# Object storage / S3 (MinIO) settings
def get_object_storage_endpoint_env():
    return os.getenv("OBJECT_STORAGE_ENDPOINT")


def get_object_storage_prefix_env():
    return os.getenv("OBJECT_STORAGE_PREFIX")


def get_object_storage_region_env():
    # Region is required by some S3 clients but often ignored by MinIO
    return os.getenv("OBJECT_STORAGE_REGION") or "us-east-1"


def get_object_storage_bucket_name_env():
    return os.getenv("OBJECT_STORAGE_BUCKET_NAME")


def get_object_storage_access_key_id_env():
    return os.getenv("OBJECT_STORAGE_ACCESS_KEY_ID")


def get_object_storage_secret_access_key_env():
    return os.getenv("OBJECT_STORAGE_SECRET_ACCESS_KEY")


def _get_object_storage_boolean(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    if value not in ("true", "false"):
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def get_object_storage_force_path_style_env() -> bool:
    return _get_object_storage_boolean("OBJECT_STORAGE_FORCE_PATH_STYLE", True)


def _get_object_storage_checksum_policy(name: str) -> str:
    value = os.getenv(name, "when_supported")
    if value not in ("when_supported", "when_required"):
        raise ValueError(f"{name} must be when_supported or when_required")
    return value


def get_object_storage_request_checksum_calculation_env() -> str:
    return _get_object_storage_checksum_policy(
        "OBJECT_STORAGE_REQUEST_CHECKSUM_CALCULATION"
    )


def get_object_storage_response_checksum_validation_env() -> str:
    return _get_object_storage_checksum_policy(
        "OBJECT_STORAGE_RESPONSE_CHECKSUM_VALIDATION"
    )


def get_object_storage_transfer_max_concurrency_env() -> int:
    name = "OBJECT_STORAGE_TRANSFER_MAX_CONCURRENCY"
    value = os.getenv(name, "10")
    if not value.isascii() or not value.isdecimal() or int(value) < 1:
        raise ValueError(f"{name} must be a positive integer")
    return int(value)


def get_object_storage_transfer_use_threads_env() -> bool:
    return _get_object_storage_boolean("OBJECT_STORAGE_TRANSFER_USE_THREADS", True)
