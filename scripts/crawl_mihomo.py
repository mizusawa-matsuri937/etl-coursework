"""为任务 2 爬虫生成 mihomo 多端口配置：每个本地端口固定走机场的一个节点，即一个出口 IP。

只用标准库，在宿主机运行（工作目录为仓库根目录）。订阅链接从 .env 的 CRAWL_SUBSCRIPTION_URL 读取，
配置写到 outputs/task2/mihomo/（已被 .gitignore 忽略），订阅链接不会进入仓库。

    python3 scripts/crawl_mihomo.py init                 # 生成只含订阅的基础配置
    mihomo -d outputs/task2/mihomo                       # 另开终端启动，保持运行
    python3 scripts/crawl_mihomo.py ports --max 10       # 读取节点、测延迟、按节点开端口并热加载
    python3 scripts/crawl_mihomo.py check                # 逐个端口查询出口 IP

配置以 JSON 写出（JSON 是 YAML 的子集，mihomo 可直接读取），避免依赖 PyYAML。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK_DIR = ROOT / "outputs" / "task2" / "mihomo"
CONFIG = WORK_DIR / "config.yaml"
PORTS_FILE = WORK_DIR / "ports.json"
CONTROLLER = "127.0.0.1:9097"
PROVIDER = "airport"
# 机场常把剩余流量、到期时间等信息做成假节点，排除掉
DEFAULT_EXCLUDE = r"剩余|到期|过期|官网|流量|重置|套餐|订阅|Expire|Traffic|Reset|网址"
DEFAULT_PREFER = r"美国|US|United States|🇺🇸|🇺🇲"


def read_env(path: Path = ROOT / ".env") -> dict[str, str]:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def base_config(subscription: str) -> dict:
    return {
        "mode": "rule",
        "log-level": "warning",
        "ipv6": False,
        "allow-lan": False,
        "external-controller": CONTROLLER,
        "proxy-providers": {
            PROVIDER: {
                "type": "http",
                "url": subscription,
                "path": f"./providers/{PROVIDER}.yaml",
                "interval": 86400,
                "header": {"User-Agent": ["clash.meta"]},
                "health-check": {"enable": False},
            },
        },
        "proxy-groups": [{"name": "ALL", "type": "select", "use": [PROVIDER]}],
        "rules": ["MATCH,DIRECT"],
    }


def write_config(config: dict) -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def api(method: str, path: str, body: dict | None = None, timeout: float = 15):
    req = urllib.request.Request(f"http://{CONTROLLER}{path}", method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def node_names() -> list[str]:
    data = api("GET", f"/providers/proxies/{PROVIDER}")
    return [p["name"] for p in data.get("proxies", [])]


def delay_ms(name: str, timeout_ms: int = 4000) -> int | None:
    query = urllib.parse.urlencode({"url": "https://www.gstatic.com/generate_204", "timeout": timeout_ms})
    try:
        # 订阅提供者里的节点不在全局 /proxies 下，要用提供者的 healthcheck 接口
        return api("GET", f"/providers/proxies/{PROVIDER}/{urllib.parse.quote(name, safe='')}/healthcheck?{query}",
                   timeout=timeout_ms / 1000 + 5)["delay"]
    except (urllib.error.URLError, KeyError, TimeoutError, OSError):
        return None


def build_port_config(subscription: str, nodes: list[str], base_port: int) -> tuple[dict, list[dict]]:
    """每个节点一个只含它自己的 select 组（用精确名字过滤），再为每个组开一个 127.0.0.1 端口。"""
    config = base_config(subscription)
    ports = []
    for i, name in enumerate(nodes):
        group = f"exit-{i + 1:02d}"
        config["proxy-groups"].append({"name": group, "type": "select", "use": [PROVIDER],
                                       "filter": f"^{re.escape(name)}$"})
        ports.append({"port": base_port + i, "group": group, "node": name})
    config["listeners"] = [{"name": p["group"], "type": "mixed", "listen": "127.0.0.1", "port": p["port"],
                            "proxy": p["group"]} for p in ports]
    return config, ports


def cmd_init(args, env) -> None:
    write_config(base_config(env["CRAWL_SUBSCRIPTION_URL"]))
    print(f"已写入 {CONFIG.relative_to(ROOT)}。另开终端运行并保持：mihomo -d {WORK_DIR.relative_to(ROOT)}")


def cmd_ports(args, env) -> None:
    try:
        names = node_names()
    except urllib.error.URLError as exc:
        sys.exit(f"连不上 mihomo 控制接口 {CONTROLLER}（{exc}）；请先运行 mihomo -d {WORK_DIR.relative_to(ROOT)}")
    exclude, prefer = re.compile(args.exclude, re.I), re.compile(args.prefer, re.I)
    include = re.compile(args.include, re.I) if args.include else None
    candidates = [n for n in names if not exclude.search(n) and (include is None or include.search(n))]
    print(f"订阅共 {len(names)} 个条目，过滤后 {len(candidates)} 个候选节点，测延迟中……")
    with ThreadPoolExecutor(8) as pool:
        delays = dict(zip(candidates, pool.map(delay_ms, candidates)))
    alive = [n for n in candidates if delays[n] is not None]
    alive.sort(key=lambda n: (not prefer.search(n), delays[n]))  # 美国节点优先，其余按延迟
    chosen = alive[:args.max]
    for n in candidates:
        mark = "选用" if n in chosen else ("超时" if delays[n] is None else "未选")
        print(f"  [{mark}] {n}  {delays[n] or '-'} ms")
    if not chosen:
        sys.exit("没有可用节点")
    config, ports = build_port_config(env["CRAWL_SUBSCRIPTION_URL"], chosen, args.base_port)
    write_config(config)
    PORTS_FILE.write_text(json.dumps(ports, ensure_ascii=False, indent=2), encoding="utf-8")
    api("PUT", "/configs?force=true", {"path": str(CONFIG)})
    urls = ",".join(f"http://host.docker.internal:{p['port']}" for p in ports)
    print(f"\n已热加载 {len(ports)} 个端口 {ports[0]['port']}–{ports[-1]['port']}。在 .env 中设置：\n"
          f"CRAWL_PROXY_URLS={urls}")


def exit_ip(port: int) -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(
        {"http": f"http://127.0.0.1:{port}", "https": f"http://127.0.0.1:{port}"}))
    try:
        with opener.open("https://api.ipify.org", timeout=15) as resp:
            return resp.read().decode().strip()
    except Exception as exc:
        return f"失败：{type(exc).__name__}"


def cmd_check(args, env) -> None:
    ports = json.loads(PORTS_FILE.read_text(encoding="utf-8"))
    with ThreadPoolExecutor(8) as pool:
        ips = list(pool.map(exit_ip, [p["port"] for p in ports]))
    for p, ip in zip(ports, ips):
        print(f"  {p['port']}  {ip:<18} {p['node']}")
    distinct = {ip for ip in ips if not ip.startswith("失败")}
    print(f"共 {len(ports)} 个端口，{len(distinct)} 个不同的出口 IP")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="生成只含订阅的基础配置")
    ports = sub.add_parser("ports", help="按节点开端口并热加载")
    ports.add_argument("--max", type=int, default=10, help="最多开多少个端口")
    ports.add_argument("--base-port", type=int, default=7801)
    ports.add_argument("--include", help="只用名字匹配该正则的节点")
    ports.add_argument("--exclude", default=DEFAULT_EXCLUDE, help="排除名字匹配该正则的条目")
    ports.add_argument("--prefer", default=DEFAULT_PREFER, help="优先选用名字匹配该正则的节点")
    sub.add_parser("check", help="逐个端口查询出口 IP")
    args = p.parse_args()

    env = read_env()
    if args.cmd != "check" and not env.get("CRAWL_SUBSCRIPTION_URL"):
        sys.exit("请先在 .env 中填写 CRAWL_SUBSCRIPTION_URL")
    {"init": cmd_init, "ports": cmd_ports, "check": cmd_check}[args.cmd](args, env)


if __name__ == "__main__":
    main()
