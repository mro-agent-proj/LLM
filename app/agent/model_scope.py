"""Validate the Backend allowance. The AI never derives authorized models itself."""

LEVELS = {"EXACT", "MODEL_GENERATION", "FAMILY"}


def validate_policy(scope: dict) -> None:
    policy = scope.get("model_scope")
    if policy is None:
        return
    if not isinstance(policy, dict) or policy.get("policy_version") != "airbus-model-scope-v1":
        raise ValueError("Unsupported model scope policy")
    levels = policy.get("levels")
    if not isinstance(levels, dict) or not set(levels) <= LEVELS:
        raise ValueError("Invalid scope levels")
    model = scope.get("model_key")
    if levels.get("EXACT") != [model] or policy.get("default_level") not in levels:
        raise ValueError("Requested model and scope policy disagree")
    for keys in levels.values():
        if (
            not isinstance(keys, list)
            or not keys
            or any(not isinstance(k, str) or not k for k in keys)
        ):
            raise ValueError("Invalid model allowance")
        if model not in keys or len(keys) != len(set(keys)):
            raise ValueError("Invalid scope membership")
    if not set(levels.get("MODEL_GENERATION", [])) <= set(levels.get("FAMILY", levels["EXACT"])):
        raise ValueError("Non-nested model allowance")


def default_level(scope: dict) -> str:
    return (scope.get("model_scope") or {}).get("default_level", "EXACT")


def allowed_models(scope: dict, level: str) -> list[str]:
    validate_policy(scope)
    levels = (scope.get("model_scope") or {}).get("levels", {"EXACT": [scope.get("model_key")]})
    if level not in levels:
        raise ValueError("Search level is outside the Backend allowance")
    return levels[level]


def relation(scope: dict, model: str) -> str:
    if model == scope["model_key"]:
        return "EXACT"
    levels = (scope.get("model_scope") or {}).get("levels", {})
    return "MODEL_GENERATION" if model in levels.get("MODEL_GENERATION", []) else "FAMILY"
