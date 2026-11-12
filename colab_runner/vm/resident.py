# The kernel-resident fallback: this cell starts the job and then stays busy until the job has ended (or
# CR_RESIDENT_MAX_S), writing <job>.poll.json every CR_POLL_EVERY_S and printing a line each time, so a kernel
# execution is active the whole time. The rig reads the poll file with `colab download`, not with exec (a busy
# kernel queues further cells).
print(cr_start_job(), flush=True)
_every = float(os.environ.get("CR_POLL_EVERY_S", "30"))
_max = float(os.environ.get("CR_RESIDENT_MAX_S", "86400"))
_t_end = time.time() + _max
while True:
    _rec, _lines = cr_collect(0)
    _tmp = CR_POLL_FILE + ".tmp"
    with open(_tmp, "w") as _fh:
        json.dump(_rec, _fh)
    os.replace(_tmp, CR_POLL_FILE)
    print("CR_RESIDENT_BEAT %.0f" % _rec["now_epoch"], flush=True)
    if _rec["rc"] is not None or time.time() >= _t_end:
        break
    time.sleep(_every)
print("CR_RESIDENT_DONE", flush=True)
