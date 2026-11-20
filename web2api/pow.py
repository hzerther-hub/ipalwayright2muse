# -*- coding: utf-8 -*-
"""DeepSeek 的 proof-of-work 求解。

DeepSeek 网页版每个 chat 请求前要先过一道 PoW：先向
``/api/v0/chat/create_pow_challenge`` 换一个挑战，再把答案带上。

挑战的形状（从网页版的 worker chunk 里读出来的）::

    {algorithm: "DeepSeekHashV1", challenge, salt, difficulty, signature, expire_at}

求解在 WebAssembly 里完成（``sha3_wasm_bg.wasm``，导出 ``wasm_solve`` 与
``wasm_deepseek_hash_v1``），输入是 ``"<challenge>_<difficulty>_"`` 和
``salt``，输出是一个 float64 作为答案。

本模块不重新实现哈希——那是 DeepSeekHashV1 的私有定义，逆向它既慢又脆。
改为**直接跑官方的 wasm**：用 Node 把 wasm 装起来求解。Node 不在的话
退回到「用浏览器里的 worker 解」，两条路都产出同样的答案。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# 官方 worker chunk 与 wasm 的地址（静态资源，带内容哈希）
WORKER_JS = "https://fe-static.deepseek.com/chat/static/37627.ebf6d8f55d.js"
WASM_URL = "https://fe-static.deepseek.com/chat/static/sha3_wasm_bg.7b9ca65ddd.wasm"

CACHE_DIR = os.path.join(REPO, "data", "deepseek")
SOLVER_JS = r"""
const fs = require('fs');
const path = require('path');

async function main() {
  const wasmPath = process.argv[2];
  const algorithm = process.argv[3];
  const prefix = process.argv[4];
  const salt = process.argv[5];
  const bytes = fs.readFileSync(wasmPath);
  const { instance } = await WebAssembly.instantiate(bytes, {});
  const ex = instance.exports;
  const mem = () => new Int32Array(ex.memory.buffer);

  const enc = new TextEncoder();
  const writeStr = (s) => {
    const data = enc.encode(s);
    const ptr = ex.__wbindgen_add_to_stack_pointer(-data.length - 8);
    const view = new Uint8Array(ex.memory.buffer, ptr, data.length);
    view.set(data);
    return { ptr, len: data.length };
  };

  // wasm_solve(stack, algPtr, algLen, prefixPtr, prefixLen, salt)
  const stack = ex.__wbindgen_add_to_stack_pointer(-16);
  const a = writeStr(algorithm);
  const p = writeStr(prefix);
  ex.wasm_solve(stack, a.ptr, a.len, p.ptr, p.len, salt);
  const m = mem();
  const status = m[stack >> 2];
  const answer = new Float64Array(ex.memory.buffer)[(stack >> 2) + 2];
  if (status !== 0) {
    console.error('no solution');
    process.exit(2);
  }
  process.stdout.write(JSON.stringify({ answer }));
}

main().catch(e => { console.error(String(e && e.stack || e)); process.exit(1); });
"""


def _node():
    return shutil.which("node") or shutil.which("nodejs")


def _fetch(url, dest):
    import urllib.request
    proxy = os.environ.get("CAPTURE_PROXY", "http://127.0.0.1:7890")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    with opener.open(req, timeout=90) as resp:
        data = resp.read()
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(data)
    return dest


def wasm_path():
    """The official wasm, cached on disk."""
    path = os.path.join(CACHE_DIR, "sha3_wasm_bg.wasm")
    if os.path.isfile(path) and os.path.getsize(path) > 1000:
        return path
    try:
        return _fetch(WASM_URL, path)
    except Exception:  # noqa: BLE001
        return None


def solve(challenge):
    """Solve a PoW challenge.

    challenge: {algorithm, challenge, salt, difficulty, signature}
    returns the answer dict to attach to the request, or None.
    """
    node = _node()
    if not node:
        return None
    wasm = wasm_path()
    if not wasm:
        return None
    algorithm = challenge.get("algorithm") or "DeepSeekHashV1"
    prefix = "%s_%s_" % (challenge.get("challenge") or "",
                         challenge.get("difficulty") or "")
    salt = challenge.get("salt")
    if salt is None:
        return None

    script = os.path.join(CACHE_DIR, "solve.js")
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(script, "w", encoding="utf-8") as f:
        f.write(SOLVER_JS)

    # salt may arrive as a number or a string; the wasm takes it as a number.
    try:
        salt_value = float(salt)
    except (TypeError, ValueError):
        salt_value = salt

    try:
        proc = subprocess.run(
            [node, script, wasm, algorithm, prefix, str(salt_value)],
            capture_output=True, timeout=180)
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0:
        return None
    try:
        out = json.loads((proc.stdout or b"").decode("utf-8", "replace"))
    except ValueError:
        return None
    return {
        "algorithm": algorithm,
        "challenge": challenge.get("challenge"),
        "salt": challenge.get("salt"),
        "answer": out.get("answer"),
        "signature": challenge.get("signature"),
    }
