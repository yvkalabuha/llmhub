import json

import pytest
from llmhub_infer.plan import PlanError, parse_plan

SHA = "a" * 64


def base() -> dict:
    return {
        "version": 1,
        "files": [
            {"id": "model", "size": 10, "sha256": SHA},
            {"id": "draft", "size": 5, "sha256": SHA},
        ],
        "sources": [
            {
                "name": "self_hosted",
                "base_url": "https://1.2.3.4:9443/w/abc/",
                "paths": {"model": "q/m.gguf", "draft": "q/d.gguf"},
                "ca_pem": "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----",
                "auth": "weights_token",
                "min_speed_mb_s": 25,
                "segments": 1,
            },
            {
                "name": "hf",
                "base_url": "https://huggingface.co/r/resolve/rev",
                "paths": {"model": "m.gguf", "draft": "MTP/d.gguf"},
                "ca_pem": None,
                "auth": "none",
                "min_speed_mb_s": 0,
                "segments": 4,
            },
        ],
        "deadline_unix": 1762300000,
        "retries_per_source": 2,
    }


def test_valid_plan() -> None:
    p = parse_plan(json.dumps(base()))
    assert [f.id for f in p.files] == ["model", "draft"]
    assert [s.name for s in p.sources] == ["self_hosted", "hf"]
    assert p.sources[1].base_url.endswith("/") and p.sources[1].segments == 4
    assert p.deadline_unix == 1762300000
    assert p.speed_grace_s == 30 and p.retries_per_source == 2


def _bad(mutate) -> None:
    d = base()
    mutate(d)
    with pytest.raises(PlanError):
        parse_plan(json.dumps(d))


def test_rejects() -> None:
    _bad(lambda d: d.update(version=2))
    _bad(lambda d: d["files"].pop(0))  # no model
    _bad(lambda d: d["files"].append({"id": "model", "size": 1, "sha256": SHA}))
    _bad(lambda d: d["files"][0].update(sha256="zz"))
    _bad(lambda d: d["files"][0].update(size=0))
    _bad(lambda d: d["sources"][0]["paths"].update(model="../x"))
    _bad(lambda d: d["sources"][0]["paths"].update(model="/abs"))
    _bad(lambda d: d["sources"][0]["paths"].pop("draft"))
    _bad(lambda d: d["sources"][0].update(base_url="http://1.2.3.4/w/"))
    _bad(lambda d: d["sources"][0].update(base_url="https://u:p@1.2.3.4/"))
    _bad(lambda d: d["sources"][0].update(auth="basic"))
    _bad(lambda d: d["sources"][0].update(segments=0))
    _bad(lambda d: d["sources"].append(dict(d["sources"][0])))  # duplicate name
    _bad(lambda d: d.update(sources=[]))


def test_not_json_and_no_secret_leak() -> None:
    with pytest.raises(PlanError):
        parse_plan("{nope")
    d = base()
    d["sources"][0]["base_url"] = "http://SECRETTOKEN@1.2.3.4/"
    with pytest.raises(PlanError) as e:
        parse_plan(json.dumps(d))
    assert "SECRETTOKEN" not in str(e.value)
