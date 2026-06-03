#!/usr/bin/env python3
"""
Тест логики retry в rent_with_retry — standalone, без загрузки бота.
Тестирует логику напрямую через воспроизведение ключевых функций.
"""
import sys, time, types, unittest, threading

# ─── Воспроизводим ключевые части бота для тестирования ─────────────────────
PROVISION_RETRIES = 6
API_KEYS = ["token_A", "token_B", "token_C"]
RATE_LIMITS = {k: 0 for k in API_KEYS}
GHOST_COOLDOWNS = {}
TOKEN_LOCK = threading.Lock()
_token_idx = 0

logs = []
def log_sys(msg): logs.append(("sys", msg))
def log_success(msg): logs.append(("ok", msg))
def tg_send(msg): logs.append(("tg", msg))
def log_missed(*a, **kw): logs.append(("missed", a))

def get_best_token():
    global _token_idx
    with TOKEN_LOCK:
        now = time.time()
        n = len(API_KEYS)
        for i in range(n):
            idx = (_token_idx + i) % n
            k = API_KEYS[idx]
            if RATE_LIMITS[k] < now:
                _token_idx = (idx + 1) % n
                return k, 0
        best = min(API_KEYS, key=lambda k: RATE_LIMITS[k])
        return best, max(0, RATE_LIMITS[best] - now)

def set_token_rate_limit(token, wait_time):
    with TOKEN_LOCK:
        RATE_LIMITS[token] = time.time() + wait_time

# SESSION mock — заменяется в каждом тесте
class FakeSession:
    def post(self, *a, **kw): raise NotImplementedError
    def delete(self, *a, **kw): pass

SESSION = FakeSession()

class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}
    def json(self): return self._body
    @property
    def text(self): return str(self._body)

BASE_URL = "https://api.digitalocean.com/v2"
PARALLEL_REGIONS = 6

def try_create_once(slug, region, user_data):
    token, wait = get_best_token()
    if wait > 0:
        return None, "All tokens rate limited globally"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        r = SESSION.post(f"{BASE_URL}/droplets",
                         json={"size": slug, "region": region},
                         headers=headers, timeout=25)
    except (ConnectionError, TimeoutError, OSError) as e:
        return None, str(e)
    if r.status_code == 429:
        wait_time = int(r.headers.get("Retry-After", 10))
        set_token_rate_limit(token, wait_time)
        return None, f"HTTP 429: rate limited, wait {wait_time}s"
    if r.status_code in (200, 201, 202):
        return r.json()["droplet"]["id"], None
    try:
        err = r.json().get("message", r.text)
    except Exception:
        err = r.text
    return None, f"HTTP {r.status_code}: {err}"

def race_create(slug, regions, user_data):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    regions = list(dict.fromkeys(regions))[:PARALLEL_REGIONS]
    winners, errors = [], []
    def attempt(region):
        now = time.time()
        if GHOST_COOLDOWNS.get((slug, region), 0) > now:
            return region, None, "micro-cooldown"
        did, err = try_create_once(slug, region, user_data)
        if err:
            el = str(err).lower()
            if "not available" in el or "capacity" in el or "unprocessable" in el:
                GHOST_COOLDOWNS[(slug, region)] = now + 30.0
        return region, did, err
    with ThreadPoolExecutor(max_workers=len(regions)) as pool:
        futures = [pool.submit(attempt, r) for r in regions]
        for fut in as_completed(futures):
            region, did, err = fut.result()
            if did: winners.append((did, region))
            elif err: errors.append(err)
    if not winners:
        return None, None, (errors[0] if errors else "unknown")
    did, region = winners[0]
    return did, region, None

# ── THIS is the function under test ──────────────────────────────────────────
def rent_with_retry(slug, label, regions, user_data, ts):
    last_err = None
    notified = False
    for tryno in range(1, PROVISION_RETRIES + 1):
        did, region, err = race_create(slug, regions, user_data)
        if not did:
            last_err = err
            err_low = str(err).lower()
            is_network_err = any(x in err_low for x in (
                "ssleofError", "eof occurred", "connection aborted",
                "remotedisconnected", "remote end closed", "read timed out",
                "connectionerror", "max retries exceeded"
            ))
            if is_network_err:
                log_sys(f"[{ts}] [~] Network error on try {tryno}, retrying… ({err_low[:80]})")
                continue
            break
        if not notified:
            notified = True
            tg_send(f"🎯 Поймал окно! {label} {region}")
        log_success(f"[{ts}] Created {label} {region} id={did} try {tryno}/{PROVISION_RETRIES}")
        ip = wait_for_active(did)
        if ip:
            return (did, region, ip), None
        last_err = f"reclaimed during provisioning (try {tryno}/{PROVISION_RETRIES})"
        log_sys(f"[{ts}] reclaimed try {tryno}")
    return None, last_err

def wait_for_active(did):
    return None  # overridden in tests


# ─── Тесты ───────────────────────────────────────────────────────────────────
class TestRetryLogic(unittest.TestCase):

    def setUp(self):
        global _token_idx, RATE_LIMITS, GHOST_COOLDOWNS
        logs.clear()
        _token_idx = 0
        for k in API_KEYS:
            RATE_LIMITS[k] = 0
        GHOST_COOLDOWNS.clear()

    def _post_sequence(self, responses):
        it = iter(responses)
        def fake_post(*a, **kw):
            r = next(it)
            if isinstance(r, Exception): raise r
            return r
        SESSION.post = fake_post

    # ── 1: Успех с первой попытки ────────────────────────────────────────────
    def test_success_first_try(self):
        self._post_sequence([FakeResponse(202, {"droplet": {"id": 1}})])
        global wait_for_active
        wait_for_active = lambda did: "1.2.3.4"

        result, err = rent_with_retry("gpu-h100x1-80gb", "H100", ["nyc2"], "", "T")
        self.assertIsNotNone(result)
        self.assertEqual(result[2], "1.2.3.4")
        print("  ✅ success_first_try")

    # ── 2: SSLEOFError → retry → успех ──────────────────────────────────────
    def test_ssl_eof_retry(self):
        ssl_e = ConnectionError("SSLEOFError(8, 'EOF occurred in violation of protocol')")
        self._post_sequence([ssl_e, ssl_e, FakeResponse(202, {"droplet": {"id": 2}})])
        global wait_for_active
        wait_for_active = lambda did: "2.3.4.5"

        result, err = rent_with_retry("gpu-h100x1-80gb", "H100", ["nyc2"], "", "T")
        self.assertIsNotNone(result, f"err={err}")
        print("  ✅ ssl_eof_retry_then_success")

    # ── 3: Connection aborted → retry → успех ───────────────────────────────
    def test_connection_aborted_retry(self):
        e = ConnectionError("('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))")
        self._post_sequence([e, FakeResponse(202, {"droplet": {"id": 3}})])
        global wait_for_active
        wait_for_active = lambda did: "3.4.5.6"

        result, err = rent_with_retry("gpu-h100x1-80gb", "H100", ["nyc2"], "", "T")
        self.assertIsNotNone(result, f"err={err}")
        print("  ✅ connection_aborted_retry")

    # ── 4: Read timed out → retry → успех ───────────────────────────────────
    def test_read_timeout_retry(self):
        e = ConnectionError("HTTPSConnectionPool: Read timed out. (read timeout=25)")
        self._post_sequence([e, FakeResponse(202, {"droplet": {"id": 4}})])
        global wait_for_active
        wait_for_active = lambda did: "4.5.6.7"

        result, err = rent_with_retry("gpu-h100x1-80gb", "H100", ["nyc2"], "", "T")
        self.assertIsNotNone(result, f"err={err}")
        print("  ✅ read_timeout_retry")

    # ── 5: Max retries exceeded → retry → успех ─────────────────────────────
    def test_max_retries_exceeded_retry(self):
        e = ConnectionError("HTTPSConnectionPool: Max retries exceeded with url: /v2/droplets")
        self._post_sequence([e, FakeResponse(202, {"droplet": {"id": 5}})])
        global wait_for_active
        wait_for_active = lambda did: "5.6.7.8"

        result, err = rent_with_retry("gpu-h100x1-80gb", "H100", ["nyc2"], "", "T")
        self.assertIsNotNone(result, f"err={err}")
        print("  ✅ max_retries_exceeded_retry")

    # ── 6: Capacity error → НЕ делает retry, сразу break ────────────────────
    def test_capacity_no_retry(self):
        calls = [0]
        def fake_post(*a, **kw):
            calls[0] += 1
            return FakeResponse(422, {"message": "Size is not available in this region"})
        SESSION.post = fake_post
        global wait_for_active
        wait_for_active = lambda did: None

        result, err = rent_with_retry("gpu-h100x1-80gb", "H100", ["nyc2"], "", "T")
        self.assertIsNone(result)
        self.assertEqual(calls[0], 1, f"Capacity error should break immediately, got {calls[0]} calls")
        print("  ✅ capacity_no_retry (break immediately)")

    # ── 7: Все 6 попыток сетевые ошибки → возвращает None ───────────────────
    def test_all_retries_exhausted(self):
        e = ConnectionError("('Connection aborted.', RemoteDisconnected('Remote end closed'))")
        self._post_sequence([e] * 6)
        global wait_for_active
        wait_for_active = lambda did: None

        result, err = rent_with_retry("gpu-h100x1-80gb", "H100", ["nyc2"], "", "T")
        self.assertIsNone(result)
        self.assertIsNotNone(err)
        retry_logs = [l for l in logs if "[~] Network error" in str(l)]
        self.assertEqual(len(retry_logs), 6, f"Should log 6 retries, got {len(retry_logs)}")
        print(f"  ✅ all_retries_exhausted (6 network fails → None, {len(retry_logs)} retries logged)")

    # ── 8: 429 → токен блокируется ──────────────────────────────────────────
    def test_429_blocks_token(self):
        SESSION.post = lambda *a, **kw: FakeResponse(
            429, {"message": "rate limited"}, {"Retry-After": "5"}
        )
        global wait_for_active
        wait_for_active = lambda did: None

        rent_with_retry("gpu-h100x1-80gb", "H100", ["nyc2"], "", "T")
        blocked = sum(1 for v in RATE_LIMITS.values() if v > time.time())
        self.assertGreater(blocked, 0, "429 should block at least one token")
        print(f"  ✅ 429_blocks_token ({blocked}/{len(API_KEYS)} tokens blocked)")

    # ── 9: Round-robin ротирует токены ──────────────────────────────────────
    def test_round_robin(self):
        global _token_idx, GHOST_COOLDOWNS
        _token_idx = 0
        used = []
        def fake_post(*a, headers=None, **kw):
            token = (headers or {}).get("Authorization", "").replace("Bearer ", "")
            used.append(token)
            # Возвращаем 422 без текста "not available" — не ставит GHOST_COOLDOWN
            return FakeResponse(422, {"message": "quota exceeded"})
        SESSION.post = fake_post

        # 3 последовательных вызова — каждый должен использовать следующий токен
        for _ in range(3):
            GHOST_COOLDOWNS.clear()  # сбросить кулдауны между вызовами
            race_create("gpu-h100x1-80gb", ["nyc2"], "")

        unique = list(dict.fromkeys(used))
        self.assertGreater(len(unique), 1, f"Round-robin should rotate tokens, got: {unique}")
        print(f"  ✅ round_robin ({len(unique)} unique tokens in 3 calls: {unique})")

    # ── 10: is_network_err pattern matching (unit) ───────────────────────────
    def test_is_network_err_patterns(self):
        patterns = (
            "ssleofError", "eof occurred", "connection aborted",
            "remotedisconnected", "remote end closed", "read timed out",
            "connectionerror", "max retries exceeded"
        )
        real_errors = [
            ("SSLEOFError(8, 'EOF occurred in violation of protocol (_ssl.c:2426)')", True),
            ("('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))", True),
            ("HTTPSConnectionPool: Read timed out. (read timeout=25)", True),
            ("HTTPSConnectionPool: Max retries exceeded with url: /v2/droplets", True),
            ("HTTP 422: Size is not available in this region", False),
            ("HTTP 422: will exceed your droplet limit", False),
            ("HTTP 429: Token rate limited, wait 10s", False),
        ]
        for err_str, expected in real_errors:
            err_low = err_str.lower()
            matched = any(x in err_low for x in patterns)
            self.assertEqual(matched, expected,
                f"Pattern match={matched} (expected {expected}) for: {err_str[:60]}")
            mark = "✅" if matched == expected else "❌"
            net = "network→retry" if matched else "capacity→break"
            print(f"  {mark} [{net}] {err_str[:60]}")


if __name__ == "__main__":
    print("=" * 62)
    print("GPU SNIPER — RETRY LOGIC TEST SUITE")
    print("=" * 62)
    suite = unittest.TestLoader().loadTestsFromTestCase(TestRetryLogic)
    import io
    runner = unittest.TextTestRunner(verbosity=0, stream=io.StringIO())
    res = runner.run(suite)
    print()
    if res.wasSuccessful():
        print(f"✅ ALL {res.testsRun} TESTS PASSED")
    else:
        print(f"❌ {len(res.failures)} FAILURES, {len(res.errors)} ERRORS")
        for test, tb in res.failures + res.errors:
            print(f"\n  FAIL: {test}")
            print(f"  {tb.strip().splitlines()[-1]}")
    print("=" * 62)
