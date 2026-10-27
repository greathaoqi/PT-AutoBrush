"""Root-only integration check using reserved documentation IPs; always unban them."""
import re
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

WEB_IP = "198.51.100.77"
SSH_IP = "198.51.100.78"
IPV6 = "2001:db8::77"


def command(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()


def client(*args):
    return command("fail2ban-client", *args)


def wait_for(predicate, seconds=20):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            if predicate():
                return
        except subprocess.CalledProcessError:
            pass
        time.sleep(0.5)
    raise AssertionError("Fail2ban integration check timed out")


def banned(jail, ip):
    return ip in client("get", jail, "banip").split()


def total_failed():
    return int(re.search(r"Total failed:\s+(\d+)", client("status", "pt-login")).group(1))


def append_attempts(first, last):
    stamp = datetime.now().astimezone().strftime("%d/%b/%Y:%H:%M:%S %z")
    with Path("/var/log/nginx/pt.access.log").open("a") as log:
        for index in range(first, last + 1):
            log.write(f'{WEB_IP} [{stamp}] "POST /api/auth/login" 401 42 0.{index:03d}\n')


try:
    stamp = datetime.now().astimezone().strftime("%d/%b/%Y:%H:%M:%S %z")
    fixtures = [
        f'{WEB_IP} [{stamp}] "POST /api/auth/login" 401 42 0.123',
        f'{WEB_IP} [{stamp}] "POST /api/auth/login" 200 11 0.123',
        f'{WEB_IP} [{stamp}] "GET /api/config" 401 42 0.123',
        f'{WEB_IP} [{stamp}] "GET /api/auth/login" 401 42 0.123',
        f'{WEB_IP} [{stamp}] "POST /api/auth/login" 403 42 0.123',
    ]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log") as test_log:
        test_log.write("\n".join(fixtures) + "\n")
        test_log.flush()
        result = command("fail2ban-regex", test_log.name, "/etc/fail2ban/filter.d/pt-login.conf")
        assert re.search(r"Lines: 5 lines, 0 ignored, 1 matched, 4 missed", result), result
    print("PASS filter matches only failed POST logins")
    for jail in ("pt-login", "sshd"):
        assert client("get", jail, "maxretry") == "5"
        assert client("get", jail, "findtime") == "600"
        assert client("get", jail, "bantime") == "172800"
    before = total_failed()
    append_attempts(1, 4)
    wait_for(lambda: total_failed() >= before + 4)
    assert not banned("pt-login", WEB_IP), "Four failures unexpectedly banned the test IP"
    print("PASS four failures do not ban")
    append_attempts(5, 5)
    wait_for(lambda: banned("pt-login", WEB_IP))
    assert WEB_IP + "/32" in command("iptables", "-S", "f2b-pt-login")
    assert any("80,443" in line and "f2b-pt-login" in line for line in command("iptables", "-S", "INPUT").splitlines())
    print("PASS fifth failure bans via firewall on ports 80/443")
    client("set", "pt-login", "banip", IPV6)
    wait_for(lambda: banned("pt-login", IPV6))
    assert IPV6 + "/128" in command("ip6tables", "-S", "f2b-pt-login")
    print("PASS IPv6 firewall ban")
    client("set", "sshd", "banip", SSH_IP)
    assert SSH_IP + "/32" in command("iptables", "-S", "f2b-sshd")
    assert any("22" in line and "f2b-sshd" in line for line in command("iptables", "-S", "INPUT").splitlines())
    print("PASS SSH firewall ban on port 22")
    command("systemctl", "restart", "fail2ban")
    wait_for(lambda: banned("pt-login", WEB_IP) and banned("pt-login", IPV6) and banned("sshd", SSH_IP))
    assert WEB_IP + "/32" in command("iptables", "-S", "f2b-pt-login")
    print("PASS active bans restored after Fail2ban restart")
finally:
    for jail, ip in (("pt-login", WEB_IP), ("pt-login", IPV6), ("sshd", SSH_IP)):
        subprocess.run(["fail2ban-client", "set", jail, "unbanip", ip], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print("PASS reserved test IPs unbanned")
print("ALL FAIL2BAN CHECKS PASSED")
