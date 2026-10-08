from celery import shared_task

from apps.planning.models import PlanningRun

from .services import build_matrix, geocode_address, update_progress


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, max_retries=3)
def geocode_address_task(self, address_id):
    from apps.customers.models import Address

    return geocode_address(Address.objects.get(pk=address_id)).pk


@shared_task(bind=True)
def build_matrix_task(self, planning_run_id):
    try:
        matrix = build_matrix(planning_run_id)
        return matrix.pk if matrix else None
    except Exception as exc:
        run = PlanningRun.objects.get(pk=planning_run_id)
        if run.status in {
            PlanningRun.Status.DISPATCHED,
            PlanningRun.Status.IN_PROGRESS,
            PlanningRun.Status.COMPLETED,
            PlanningRun.Status.CANCELLED,
        }:
            raise
        run.status = PlanningRun.Status.FAILED
        run.error_code = "MATRIX_FAILED"
        run.error_message = str(exc)[:2000]
        run.save(update_fields=["status", "error_code", "error_message", "updated_at"])
        update_progress(run, run.progress_percent, "failed", run.error_message)
        raise
