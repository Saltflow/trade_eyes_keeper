import urllib.request, time
for attempt in range(5):
    try:
        r = urllib.request.urlopen("http://localhost:1933/health", timeout=5)
        print("OK", r.read().decode())
        break
    except Exception as e:
        print("attempt %d: %s" % (attempt, e))
        time.sleep(5)
