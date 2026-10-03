from agent_guardrails.idempotency import args_hash, idempotency_key, normalize_args


def test_arg_noise_maps_to_same_key():
    a = {"title": "x", "task_id": 123, "note": None, "meta": {"b": "1", "a": None}}
    b = {"meta": {"b": 1}, "task_id": "123", "title": "x"}
    assert idempotency_key("r", "update_task", a) == idempotency_key("r", "update_task", b)
    assert args_hash(a) == args_hash(b)


def test_real_differences_change_the_key():
    base = idempotency_key("r", "t", {"x": 1})
    assert base != idempotency_key("r", "t", {"x": 2})
    assert base != idempotency_key("r2", "t", {"x": 1})
    assert base != idempotency_key("r", "t2", {"x": 1})
    assert normalize_args({"x": True}) == {"x": True}  # bools are not numbers
