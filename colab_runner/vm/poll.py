_rec, _lines = cr_collect(int(os.environ.get("CR_POLL_LOG_OFFSET", "0") or 0))
for _ln in _lines:
    print("CR_LOG " + _ln)
print("CR_POLL " + json.dumps(_rec), flush=True)
