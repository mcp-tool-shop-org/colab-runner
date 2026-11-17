# The trip cell: terminate the job's whole process group (SIGTERM, then SIGKILL after CR_KILL_GRACE_S), before the
# runner salvages and stops the session.
print(cr_kill_job(float(os.environ.get("CR_KILL_GRACE_S", "5") or 5)), flush=True)
