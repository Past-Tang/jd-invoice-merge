"""Batch invoice merge execution.

Reads merge plan, submits each invoice via CDP + Vue VM manipulation.
Features: error handling, random delays, retry, progress tracking (resume).
"""
import asyncio
import json
import os
import random
import time
import logging
from datetime import datetime

import websockets

from core.cdp import run_js, send_cdp, drain_messages
from core.config import get_config
from core.connection import JDConnection


def setup_logging(log_file):
    """Configure logging to both console and file."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file, encoding="utf-8"),
        ]
    )
    return logging.getLogger("batch_merge")


def load_progress(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"completed": [], "failed": [], "skipped": []}


def save_progress(progress, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


async def submit_one_invoice(ws, mid, invoice, all_orders_map, config, log, attempt=1):
    """Submit a single merged invoice. Returns (success, message)."""
    org_id = invoice["org_id"]
    order_ids = invoice["order_ids"]
    total = invoice["total"]
    ivc_cfg = config["invoice"]

    # Gather originalOrderInfo for selected orders
    selected = []
    for oid in order_ids:
        order = all_orders_map.get(oid)
        if not order:
            return False, f"Order {oid} not found in data"
        if not order.get("originalOrderInfo"):
            return False, f"Order {oid} missing originalOrderInfo"
        selected.append(order)

    inject_json = json.dumps([{
        "orderId": o["orderId"],
        "originalOrderInfo": o["originalOrderInfo"],
        "orgId": o.get("orgId"),
        "ivcAmount": o.get("ivcAmount"),
    } for o in selected], ensure_ascii=False)

    # STEP A: Ensure on order list page
    url = await run_js(ws, "location.href", mid)
    if 'changeSuccess' in str(url) or 'ivcTitle' in str(url):
        log.info("  Navigating back to order list...")
        await run_js(ws, "location.href='https://invoice-m.jd.com/#/orderList?sourceId=0'", mid)
        await asyncio.sleep(3)
        url = await run_js(ws, "location.href", mid)

    if 'orderList' not in str(url) and 'hkList' not in str(url):
        log.warning(f"  Unexpected URL: {url}, trying direct navigation...")
        await run_js(ws, "location.href='https://invoice-m.jd.com/#/orderList?sourceId=0'", mid)
        await asyncio.sleep(3)

    # STEP B: Switch to 换开 tab, check any checkbox, click submit
    await run_js(ws, """
        var tab = document.querySelector('.tab-title-item.change');
        if (tab) tab.click();
    """, mid)
    await asyncio.sleep(2)

    await run_js(ws, """
        var cb = document.querySelector('.order-box-item input[type=checkbox]');
        if (cb && !cb.checked) cb.click();
    """, mid)
    await asyncio.sleep(1)

    await run_js(ws, """
        var btn = document.querySelector('button.nut-button.primary');
        if (btn) btn.click();
    """, mid)
    await asyncio.sleep(4)

    # STEP C: Verify on form page, find Vue VM
    form_url = await run_js(ws, "location.href", mid)
    if 'ivcTitle' not in str(form_url) and 'HksAppIvcTitle' not in str(form_url):
        return False, f"Failed to reach form page, URL: {form_url}"

    vm_ok = await run_js(ws, r"""
        (function() {
            var els = document.querySelectorAll('*');
            for (var i = 0; i < els.length; i++) {
                var vm = els[i].__vue__;
                if (vm && vm.$options && vm.$options.methods &&
                    vm.$options.methods.commitBatchHkfpReq) {
                    window.__vm = vm;
                    return true;
                }
            }
            return false;
        })()
    """, mid)
    if not vm_ok:
        return False, "Vue VM not found on form page"

    # STEP D: Enable Network monitoring
    await send_cdp(ws, "Network.enable", {}, mid)
    await asyncio.sleep(0.3)
    await drain_messages(ws)

    # STEP E: Set form + inject orders + call commit
    ivc_title = ivc_cfg["ivc_title"]
    result = await run_js(ws, f"""
        (function() {{
            var vm = window.__vm;
            if (!vm) return 'no_vm';
            vm.formData.invoiceModelType = 2;
            vm.formData.ivcTitleType = {ivc_cfg['ivc_title_type']};
            vm.formData.ivcType = {ivc_cfg['ivc_type']};
            vm.formData.ivcContent = {ivc_cfg['ivc_content']};
            vm.formData.changeReason = '{ivc_cfg['change_reason']}';
            if (vm.formData.self) vm.formData.self.ivcTitle = '{ivc_title}';
            var orders = {inject_json};
            try {{
                vm.commitBatchHkfpReq(orders);
                return 'OK';
            }} catch(e) {{
                return 'commit_error: ' + e.message;
            }}
        }})()
    """, mid)

    if result != 'OK':
        return False, f"commitBatchHkfpReq failed: {result}"

    # STEP F: Wait for checkMergeHkfpReq server response
    check_ok = False
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=1)
            msg = json.loads(raw)
            if msg.get("method") == "Network.responseReceived":
                resp_url = msg["params"]["response"].get("url", "")
                if "checkMerge" in resp_url:
                    status = msg["params"]["response"].get("status")
                    log.info(f"  checkMergeHkfpReq: HTTP {status}")
                    check_ok = (status == 200)
                    break
        except asyncio.TimeoutError:
            pass

    if not check_ok:
        return False, "checkMergeHkfpReq timeout or failed"

    # STEP G: Wait for groupList, then call submitMerge directly
    confirm = 'not_ready'
    for wait_i in range(10):
        await asyncio.sleep(1.5)
        confirm = await run_js(ws, r"""
            (function() {
                var vm = window.__vm;
                if (!vm) return 'no_vm';
                if (vm.groupList && vm.groupList.length > 0) {
                    vm.submitMerge();
                    return 'submitMerge_ok';
                }
                return 'waiting_groupList';
            })()
        """, mid)
        if confirm == 'submitMerge_ok':
            log.info(f"  submitMerge() called (after {(wait_i+1)*1.5:.1f}s)")
            break

    if confirm != 'submitMerge_ok':
        return False, f"submitMerge failed: {confirm}"

    # STEP H: Wait for appDoMergeHkfpReq response
    merge_result = None
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=1)
            msg = json.loads(raw)
            method = msg.get("method", "")

            if method == "Network.responseReceived":
                resp_url = msg["params"]["response"].get("url", "")
                if "appDoMerge" in resp_url or "DoMerge" in resp_url:
                    rid = msg["params"]["requestId"]
                    mid[0] += 1
                    await ws.send(json.dumps({
                        "id": mid[0],
                        "method": "Network.getResponseBody",
                        "params": {"requestId": rid}
                    }))

            elif msg.get("id") and "body" in msg.get("result", {}):
                body = msg["result"]["body"]
                try:
                    merge_result = json.loads(body)
                except Exception:
                    merge_result = {"raw": body[:200]}
                break
        except asyncio.TimeoutError:
            pass

    if not merge_result:
        page_text = await run_js(ws, "document.body.innerText.substring(0, 200)", mid)
        if '已申请' in str(page_text):
            return True, "Success (detected from page text)"
        return False, "appDoMergeHkfpReq timeout"

    if merge_result.get("code") == 0 and merge_result.get("data", {}).get("allSuccess"):
        return True, "allSuccess=true"
    else:
        return False, f"Server rejected: {json.dumps(merge_result, ensure_ascii=False)[:200]}"


async def batch_merge(ws_url):
    """Main batch merge loop."""
    config = get_config()
    paths = config["paths"]
    exec_cfg = config["execution"]

    log = setup_logging(paths["log_file"])
    mid = [0]

    with open(paths["merge_plan_file"], "r", encoding="utf-8") as f:
        plan = json.load(f)
    with open(paths["all_orders_file"], "r", encoding="utf-8") as f:
        all_orders = json.load(f)
    all_orders_map = {o["orderId"]: o for o in all_orders}

    invoices = plan["invoices"]
    progress = load_progress(paths["merge_progress_file"])
    completed_set = set(
        tuple(sorted(inv["order_ids"])) for inv in progress["completed"]
    )

    initial_completed = len(progress["completed"])
    log.info(f"Plan: {len(invoices)} invoices")
    log.info(f"Already completed: {initial_completed}")
    remaining = [inv for inv in invoices
                 if tuple(sorted(inv["order_ids"])) not in completed_set]
    log.info(f"Remaining: {len(remaining)} invoices")

    if not remaining:
        log.info("All invoices already completed!")
        return

    async with websockets.connect(ws_url, max_size=50_000_000) as ws:
        for idx, invoice in enumerate(remaining):
            inv_num = idx + 1 + initial_completed
            total_num = len(invoices)
            org_id = invoice["org_id"]
            total = invoice["total"]
            n_orders = len(invoice["order_ids"])

            log.info(f"\n{'='*50}")
            log.info(f"Invoice {inv_num}/{total_num}: orgId={org_id}, "
                     f"{n_orders} orders, ¥{total:.2f}")
            log.info(f"{'='*50}")

            success = False
            message = ""

            for attempt in range(1, exec_cfg["retry_limit"] + 1):
                if attempt > 1:
                    delay = random.uniform(3, 6)
                    log.info(f"  Retry {attempt}/{exec_cfg['retry_limit']} after {delay:.1f}s...")
                    await asyncio.sleep(delay)

                try:
                    success, message = await submit_one_invoice(
                        ws, mid, invoice, all_orders_map, config, log, attempt)
                except Exception as e:
                    message = f"Exception: {e}"
                    log.error(f"  {message}")
                    success = False

                if success:
                    break

            record = {
                "org_id": org_id,
                "order_ids": invoice["order_ids"],
                "total": total,
                "time": datetime.now().isoformat(),
                "message": message,
            }

            if success:
                log.info(f"  SUCCESS: {message}")
                progress["completed"].append(record)
            else:
                log.error(f"  FAILED: {message}")
                progress["failed"].append(record)

            save_progress(progress, paths["merge_progress_file"])

            if idx < len(remaining) - 1:
                delay = random.uniform(exec_cfg["delay_min"], exec_cfg["delay_max"])
                log.info(f"  Waiting {delay:.1f}s before next...")
                await asyncio.sleep(delay)

    log.info(f"\n{'='*50}")
    log.info(f"BATCH COMPLETE")
    log.info(f"  Completed: {len(progress['completed'])}")
    log.info(f"  Failed:    {len(progress['failed'])}")
    log.info(f"  Total amount: ¥{sum(inv['total'] for inv in progress['completed']):.2f}")
    log.info(f"{'='*50}")


def main():
    """Entry point with proper Frida lifecycle."""
    config = get_config()
    os.makedirs(config["paths"]["data_dir"], exist_ok=True)

    from core.bridge import attach_and_enable_debug
    from core.cdp import setup_port_forward, find_invoice_page
    import time

    log = setup_logging(config["paths"]["log_file"])

    device, session, script, pid = attach_and_enable_debug()
    log.info(f"JD PID: {pid}")
    time.sleep(5)

    setup_port_forward(pid)

    try:
        ws_url = find_invoice_page()
        if not ws_url:
            log.error("Invoice page not found in CDP!")
            return
        log.info(f"CDP connected: {ws_url[:80]}")
        asyncio.run(batch_merge(ws_url))
    except Exception as e:
        log.error(f"Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        script.unload()
        session.detach()
        log.info("Frida session closed.")


if __name__ == "__main__":
    main()
