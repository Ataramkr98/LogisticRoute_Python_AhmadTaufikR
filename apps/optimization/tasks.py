from celery import shared_task

from apps.planning.models import PlanningRun
from apps.routing.services import update_progress

from .services import optimize_run


@shared_task(bind=True)
def optimize_run_task(self, planning_run_id):
    PlanningRun.objects.filter(pk=planning_run_id).update(celery_task_id=self.request.id)
    try:
        plan = optimize_run(planning_run_id)
        return plan.pk if plan else None
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
        run.error_code = "OPTIMIZATION_FAILED"
        run.error_message = str(exc)[:2000]
        run.save(update_fields=["status", "error_code", "error_message", "updated_at"])
        update_progress(run, run.progress_percent, "failed", run.error_message)
        raise
