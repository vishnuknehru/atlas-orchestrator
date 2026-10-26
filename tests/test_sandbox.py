"""Test sandbox: status checks, the container contract and the canary, with a scripted command runner."""

from __future__ import annotations

import json
import plistlib
import subprocess
from pathlib import Path
from typing import Any

import pytest

from atlas.sandbox import APPROVED_HELPER_IMAGE, SandboxPaths, sandbox_status, start_command
from atlas.sandbox.checks import GIB
from atlas.sandbox.selftest import contract_args, parse_canary, run_self_test, verify_contract

GOOD_CANARY = "net lo \nuid 10001\nsocket none\nwork readonly\nrootfs readonly\ndone\n"


def apfs(*, quota: int = 80 * GIB, reserve: int | None = None, name: str = "AtlasSandbox") -> str:
    volume = {"Name": name, "CapacityQuota": quota, "CapacityReserve": quota if reserve is None else reserve}
    return plistlib.dumps({"Containers": [{"Volumes": [{"Name": "Macintosh HD"}, volume]}]}).decode()


def inspected(volume: str, execution_id: str, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "HostConfig": {
            "NetworkMode": "none", "CapDrop": ["ALL"], "CapAdd": None, "Privileged": False, "ReadonlyRootfs": True,
            "SecurityOpt": ["no-new-privileges"], "PidsLimit": 128, "Memory": 256 * 1024**2, "NanoCpus": 10**9,
            "Devices": [], "Binds": None, "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=16m"},
        },
        "Config": {
            "User": "10001:10001", "Image": APPROVED_HELPER_IMAGE,
            "Labels": {"atlas.execution_id": execution_id, "atlas.role": "test"},
        },
        "Mounts": [{"Type": "volume", "Name": volume, "Destination": "/work", "RW": False}],
    }
    for key, value in overrides.items():
        section, _, field = key.partition("__")
        data[section][field] = value
    return data


class FakeRunner:
    def __init__(self, tmp: Path, *, plist: str | None = None, vm: str = "running", mounts: str = "", image: bool = True,
                 canary: str = GOOD_CANARY, inspect_overrides: dict[str, Any] | None = None) -> None:
        self.paths = SandboxPaths(volume=tmp / "AtlasSandbox")
        self.paths.colima_home.mkdir(parents=True)
        self.plist = plist if plist is not None else apfs()
        self.vm, self.mounts, self.image, self.canary = vm, mounts, image, canary
        self.inspect_overrides = inspect_overrides or {}
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.created: list[str] = []

    def __call__(self, argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        self.envs.append(env)

        def ok(out: str = "") -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, out, "")

        if argv[:3] == ["diskutil", "apfs", "list"]:
            return ok(self.plist)
        if argv[0] == "colima" and argv[1] == "list":
            return ok(json.dumps({"name": "atlas", "status": self.vm.capitalize()}) + "\n")
        if argv[0] == "colima" and argv[1] == "ssh":
            return ok(self.mounts or "overlay / overlay rw 0 0\n")
        assert argv[:3] == ["docker", "-H", self.paths.docker_endpoint], argv
        sub = argv[3:]
        if sub[0] == "version":
            return ok("27.0.1\n")
        if sub[:2] == ["image", "inspect"]:
            return ok("sha256:abc\n") if self.image else subprocess.CompletedProcess(argv, 1, "", "no such image")
        if sub[0] == "create":
            self.created.append(sub[2])
            return ok("cid\n")
        if sub[0] == "inspect":
            name = sub[1]
            execution_id = name.removeprefix("atlas-").removesuffix("-test")
            return ok(json.dumps([inspected(f"src-{execution_id}", execution_id, **self.inspect_overrides)]))
        if sub[0] == "start":
            return ok(self.canary)
        return ok()

    def docker(self) -> list[list[str]]:
        return [c[3:] for c in self.calls if c[0] == "docker"]


def test_status_ready_when_every_check_passes(tmp_path: Path) -> None:
    runner = FakeRunner(tmp_path)
    status = sandbox_status(runner.paths, runner)
    assert status.ready, [c for c in status.checks if not c.ok]
    assert [c.id for c in status.checks] == ["storage", "data_path", "vm_running", "no_host_mounts", "daemon", "image"]
    # the sandbox is only ever addressed by its explicit endpoint, never an ambient docker context
    assert all("DOCKER_HOST" not in env for env in runner.envs)
    assert all(env.get("COLIMA_HOME") == str(runner.paths.colima_home) for argv, env in zip(runner.calls, runner.envs, strict=True) if argv[0] == "colima")


@pytest.mark.parametrize(
    ("kwargs", "failing", "fragment"),
    [
        ({"plist": apfs(name="Other")}, "storage", "No APFS volume named AtlasSandbox"),
        ({"plist": apfs(reserve=0)}, "storage", "reserve equal to quota"),
        ({"plist": apfs(quota=10 * GIB)}, "storage", "at least 80 GB"),
        ({"vm": "stopped"}, "vm_running", "Stopped"),
        ({"mounts": "mount0 /Users/me virtiofs rw 0 0\n"}, "no_host_mounts", "/Users/me"),
        ({"mounts": "host /mnt 9p rw 0 0\n"}, "no_host_mounts", "/mnt"),
    ],
)
def test_status_fails_closed_and_stops_at_the_first_failure(tmp_path: Path, kwargs: dict[str, Any], failing: str, fragment: str) -> None:
    runner = FakeRunner(tmp_path, **kwargs)
    status = sandbox_status(runner.paths, runner)
    assert not status.ready
    first = next(c for c in status.checks if not c.ok)
    assert first.id == failing and fragment in first.detail
    later = status.checks[[c.id for c in status.checks].index(failing) + 1 :]
    assert all(c.detail == "not checked yet" for c in later)


def test_data_path_must_exist_on_the_volume(tmp_path: Path) -> None:
    runner = FakeRunner(tmp_path)
    runner.paths.colima_home.rmdir()
    status = sandbox_status(runner.paths, runner)
    assert next(c for c in status.checks if not c.ok).id == "data_path"


def test_start_command_shares_nothing_and_keeps_the_docker_context() -> None:
    cmd = start_command(SandboxPaths(volume=Path("/Volumes/AtlasSandbox")))
    assert cmd.startswith("COLIMA_HOME=/Volumes/AtlasSandbox/colima colima start atlas")
    assert "--mount none" in cmd and "--activate=false" in cmd and "--disk 60" in cmd


def test_contract_args_enforce_the_container_contract() -> None:
    args = contract_args("ex1", "test", "src-ex1")
    joined = " ".join(args)
    for fragment in ("--network none", "--user 10001:10001", "--cap-drop ALL", "--security-opt no-new-privileges", "--read-only",
                     "-v src-ex1:/work:ro", "--label atlas.execution_id=ex1", APPROVED_HELPER_IMAGE):
        assert fragment in joined
    assert "--privileged" not in joined and "--network host" not in joined


def test_verify_contract_accepts_the_contract_and_names_every_deviation() -> None:
    assert verify_contract(inspected("v", "e"), "v", "e", "test") == []
    bad = inspected("v", "e")
    bad["HostConfig"].update(NetworkMode="bridge", Privileged=True, ReadonlyRootfs=False, CapDrop=None)
    bad["Config"]["User"] = "0"
    bad["Mounts"] = [{"Type": "bind", "Source": "/Users", "Destination": "/work", "RW": True}]
    problems = " | ".join(verify_contract(bad, "v", "e", "test"))
    for fragment in ("network", "capabilities", "privileged", "root filesystem", "user", "mounts"):
        assert fragment in problems


def test_parse_canary_flags_each_violation() -> None:
    assert all(c.ok for c in parse_canary(GOOD_CANARY))
    bad = "net lo eth0 \nuid 0\nsocket present\nwork writable\nrootfs writable\nhostpath /Users\n"
    failed = {c.id for c in parse_canary(bad) if not c.ok}
    assert failed == {"canary_complete", "canary_network", "canary_user", "canary_socket",
                      "canary_work_readonly", "canary_rootfs_readonly", "canary_no_host_paths"}


def test_selftest_happy_path_cleans_up(tmp_path: Path) -> None:
    runner = FakeRunner(tmp_path)
    checks = run_self_test(runner.paths, runner)
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]
    verbs = [c[0] for c in runner.docker()]
    assert verbs.index("create") < verbs.index("inspect") < verbs.index("start")  # verified before it starts
    assert ["rm", "-f", runner.created[0]] in runner.docker()
    assert any(c[:3] == ["volume", "rm", "-f"] for c in runner.docker())


def test_selftest_never_starts_a_container_that_breaks_the_contract(tmp_path: Path) -> None:
    runner = FakeRunner(tmp_path, inspect_overrides={"HostConfig__NetworkMode": "bridge"})
    checks = run_self_test(runner.paths, runner)
    contract = next(c for c in checks if c.id == "contract")
    assert not contract.ok and "network" in contract.detail
    assert "start" not in [c[0] for c in runner.docker()]
    assert ["rm", "-f", runner.created[0]] in runner.docker()  # still cleaned up


def test_selftest_does_not_touch_docker_when_the_vm_is_unsound(tmp_path: Path) -> None:
    runner = FakeRunner(tmp_path, mounts="m /Users virtiofs rw 0 0\n")
    checks = run_self_test(runner.paths, runner)
    assert not all(c.ok for c in checks)
    assert "create" not in [c[0] for c in runner.docker()]


def test_selftest_pulls_the_image_by_digest_when_missing(tmp_path: Path) -> None:
    runner = FakeRunner(tmp_path, image=False)
    checks = run_self_test(runner.paths, runner)
    assert ["pull", "--quiet", APPROVED_HELPER_IMAGE] in runner.docker()
    assert all(c.ok for c in checks)


def test_api_exposes_status_and_selftest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from atlas.api import app as app_mod
    from atlas.config.secret_store import InMemorySecretStore

    runner = FakeRunner(tmp_path)
    monkeypatch.setattr(app_mod, "sandbox_status", lambda: sandbox_status(runner.paths, runner))
    monkeypatch.setattr(app_mod, "run_self_test", lambda: run_self_test(runner.paths, runner))
    app = app_mod.create_app(data_dir=tmp_path / "home", secret_store=InMemorySecretStore(), allow_testserver=True, acquire_lock=False)
    client = TestClient(app)
    assert client.get(f"/auth/bootstrap?token={app.state.session_token}", follow_redirects=False).status_code == 303
    body = client.get("/api/v1/sandbox").json()
    assert body["ready"] is True and "diskutil apfs addVolume" in body["volume_command"]
    headers = {"X-Atlas-CSRF": app.state.csrf_token, "Origin": "http://127.0.0.1:8420"}
    result = client.post("/api/v1/sandbox/selftest", headers=headers).json()
    assert result["ready"] is True and any(c["id"] == "canary_network" for c in result["checks"])
