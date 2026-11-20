"""Celery is optional for stapel-gdpr.

With celery installed every job is a celery task under the name it always had
(``get_gdpr_beat_schedule`` references those names). Without it the jobs are
:class:`stapel_gdpr.tasks.InlineTask`: ``delay`` and ``apply_async`` run them
in the calling process. The package used to import celery at module level and
declare it as a hard dependency for that import.
"""
import subprocess
import sys
import tomllib
from pathlib import Path

from stapel_gdpr import tasks

REPO = Path(__file__).resolve().parent.parent

NAMES = {
    "run_data_export": "stapel_gdpr.tasks.run_data_export",
    "sweep_pending_exports": "stapel_gdpr.tasks.sweep_pending_exports",
    "purge_expired_exports": "stapel_gdpr.tasks.purge_expired_exports",
    "sweep_deletion_deadlines": "stapel_gdpr.tasks.sweep_deletion_deadlines",
    "probe_data_owners": "stapel_gdpr.tasks.probe_data_owners",
    "sweep_dsar_deadlines": "stapel_gdpr.tasks.sweep_dsar_deadlines",
    "process_expired_grace_periods": "stapel_gdpr.tasks.process_expired_grace_periods",
    "check_inactive_accounts": "stapel_gdpr.tasks.check_inactive_accounts",
    "run_retention_cleanup": "stapel_gdpr.tasks.run_retention_cleanup",
}

_WITHOUT_CELERY = (
    "import sys; sys.modules['celery'] = None; "
    "from stapel_gdpr._codegen import _configure; _configure(); "
    "from stapel_gdpr import tasks; "
    f"names = {NAMES!r}; "
    "jobs = [getattr(tasks, attr) for attr in names]; "
    "assert all(isinstance(job, tasks.InlineTask) for job in jobs), jobs; "
    "assert {attr: getattr(tasks, attr).name for attr in names} == names; "
    "print('inline')"
)


def test_celery_is_not_a_declared_dependency():
    deps = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["dependencies"]
    assert not [d for d in deps if d.split(">")[0].split("=")[0].strip() == "celery"]


def test_without_celery_every_job_is_inline_under_its_name(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-c", _WITHOUT_CELERY],
        cwd=str(tmp_path), capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip().splitlines()[-1] == "inline"


def test_with_celery_every_job_is_a_task_under_its_name():
    for attr, name in NAMES.items():
        job = getattr(tasks, attr)
        assert not isinstance(job, tasks.InlineTask), attr
        assert job.name == name


def test_an_inline_job_runs_now_and_ignores_routing():
    calls = []

    def job(a, b=0):
        calls.append((a, b))
        return a + b

    inline = tasks.InlineTask(job, "x.job")
    assert inline(1, b=2) == 3
    assert inline.delay(2, b=3) == 5
    assert inline.apply_async(args=[3], kwargs={"b": 4}, queue="thumbnails") == 7
    assert calls == [(1, 2), (2, 3), (3, 4)]
    assert inline.name == "x.job" and inline.__name__ == "job"



_INLINE_EXPORT_FAILURE = """
import sys
sys.modules['celery'] = None
from stapel_gdpr._codegen import _configure
_configure()
from stapel_gdpr import tasks
calls = []
def boom(request_id):
    calls.append(request_id)
    raise RuntimeError('storage down')
tasks._run_data_export = boom
assert tasks.run_data_export.delay(7) is None
assert calls == [7]
print('logged')
"""


def test_inline_export_failure_is_logged_not_raised(tmp_path):
    """Inline there is no retry ladder: the request stays pending for the sweep."""
    proc = subprocess.run(
        [sys.executable, "-c", _INLINE_EXPORT_FAILURE],
        cwd=str(tmp_path), capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip().splitlines()[-1] == "logged"
    assert "run_data_export failed [request=7]" in proc.stderr
