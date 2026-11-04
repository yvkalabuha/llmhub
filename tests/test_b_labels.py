from llmhub.provider_adapters.labels import make_label, new_op_id, parse_label, verify_label

SECRET = b"s" * 32


def test_format_and_verify() -> None:
    op = new_op_id()
    label = make_label(op, SECRET)
    assert label.startswith(f"llmhub-{op}-") and len(op) == 12
    assert parse_label(label) == (op, label.rsplit("-", 1)[1])
    assert verify_label(label, SECRET)


def test_forged_hmac_and_wrong_secret() -> None:
    label = make_label("0123456789ab", SECRET)
    forged = label[:-8] + "0" * 8
    assert not verify_label(forged, SECRET)
    assert not verify_label(label, b"t" * 32)


def test_foreign_prefix_is_none() -> None:
    assert parse_label("other-0123456789ab-01234567") is None
    assert parse_label("llmhub-xyz-01234567") is None
    assert not verify_label("my-gpu", SECRET)
