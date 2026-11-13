from tests.test_e2e import live  # noqa: F401
from tests.review_n_boundary_checks import (
    assert_analysis_preparation_releases_locks,
    assert_unknown_job_audit_survives_material_deletion,
)


def test_analysis_http_poll_during_preparation(live):  # noqa: F811
    assert_analysis_preparation_releases_locks(live)


def test_deleted_material_keeps_unknown_audit(live):  # noqa: F811
    assert_unknown_job_audit_survives_material_deletion(live)
