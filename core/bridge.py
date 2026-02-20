"""Frida bridge for JD App WebView debugging.

Handles Frida 17.x Java bridge loading and D6 Chromium WebView debug activation.
"""
import os
import json
import frida
import frida_tools
from itertools import count


def build_frida_script(js_code: str) -> str:
    """Build a Frida script with Java bridge for Frida 17.x.

    Frida 17.x decoupled the Java bridge from core, so we must manually
    load java.js and prepend it to the user script.

    Args:
        js_code: JavaScript code to execute in the Frida context.

    Returns:
        Packaged script string ready for session.create_script().
    """
    bridge_path = os.path.join(
        os.path.dirname(frida_tools.__file__), 'bridges', 'java.js'
    )
    with open(bridge_path, 'r', encoding='utf-8') as f:
        bridge_code = f.read()

    bridge_code += "\nObject.defineProperty(globalThis, 'Java', { value: bridge });"
    wrapper = f'Script.evaluate("u", {json.dumps(js_code)});'

    counter = count(1)
    parts = []
    for fragment in [bridge_code, wrapper]:
        idx = next(counter)
        size = len(fragment.encode('utf-8'))
        parts.append(f'{size} /frida/repl-{idx}.js\n\u2704\n{fragment}')

    return '\U0001f4e6\n' + '\n\u2704\n'.join(parts)


# Frida JS to enable D6 WebView debugging
ENABLE_D6_DEBUG_JS = r"""
Java.perform(function() {
    Java.choose("com.jd.libs.xwin.widget.XWebView", {
        onMatch: function(inst) {
            Java.scheduleOnMainThread(function() {
                try { inst.enableWebContentsDebug(true); } catch(e) {}
            });
        },
        onComplete: function() { send("ready"); }
    });
});"""


def attach_and_enable_debug(on_message=None):
    """Attach Frida to JD App and enable D6 WebView debugging.

    Args:
        on_message: Optional callback for Frida messages.

    Returns:
        Tuple of (frida_device, frida_session, frida_script, jd_pid).
    """
    import subprocess

    pid_result = subprocess.run(
        ["adb", "shell", "pidof com.jingdong.app.mall"],
        capture_output=True, text=True
    )
    pid = int(pid_result.stdout.strip())

    device = frida.get_usb_device(timeout=5)
    session = device.attach(pid)
    script = session.create_script(build_frida_script(ENABLE_D6_DEBUG_JS))

    if on_message:
        script.on('message', on_message)
    else:
        script.on('message', _default_on_message)

    script.load()
    return device, session, script, pid


def _default_on_message(msg, data):
    if msg['type'] == 'send':
        print(f"[frida] {msg['payload']}")
