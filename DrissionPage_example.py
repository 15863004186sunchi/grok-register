from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import PageDisconnectedError
import argparse
import shutil
import tempfile
import datetime
import logging
import time
import os
import secrets
import sys

import re
import json
from email_register import get_email_and_token, get_oai_code
from YesCaptcha_service import TurnstileService


def setup_run_logger() -> logging.Logger:
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"run_{ts}.log")

    logger = logging.getLogger("grok_register")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger.info("日志文件: %s", log_path)
    return logger


run_logger: logging.Logger = None



def ensure_stable_python_runtime():
    # 优先自动切到更稳定的 3.12 / 3.13，避免 3.14 下 Mail.tm 偶发 TLS/兼容问题。
    if sys.version_info < (3, 14) or os.environ.get("DPE_REEXEC_DONE") == "1":
        return

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(local_app_data, "Programs", "Python", "Python313", "python.exe"),
    ]

    current_python = os.path.normcase(os.path.abspath(sys.executable))
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        if os.path.normcase(os.path.abspath(candidate)) == current_python:
            return

        print(f"[*] 检测到 Python {sys.version.split()[0]}，自动切换到更稳定的解释器: {candidate}")
        env = os.environ.copy()
        env["DPE_REEXEC_DONE"] = "1"
        os.execve(candidate, [candidate, os.path.abspath(__file__), *sys.argv[1:]], env)


def warn_runtime_compatibility():
    # 中文提示：避免把底层 TLS 兼容问题误判成脚本逻辑错误。
    if sys.version_info >= (3, 14):
        print("[提示] 当前 Python 为 3.14+；若出现 Mail.tm TLS 异常，建议改用 Python 3.12 或 3.13。")


ensure_stable_python_runtime()
warn_runtime_compatibility()

# 无头服务器自动启用 Xvfb 虚拟显示器
_virtual_display = None
if not os.environ.get("DISPLAY") or os.environ.get("USE_XVFB") == "1":
    try:
        from pyvirtualdisplay import Display
        _virtual_display = Display(visible=0, size=(1920, 1080))
        _virtual_display.start()
        print(f"[*] Xvfb 虚拟显示器已启动: {os.environ.get('DISPLAY')}")
    except Exception as e:
        print(f"[Warn] Xvfb 启动失败: {e}，将尝试直接运行")

co = ChromiumOptions()
co.auto_port()
co.set_argument("--no-sandbox")
co.set_argument("--disable-gpu")
co.set_argument("--disable-dev-shm-usage")
co.set_argument("--disable-software-rasterizer")

# 从 config.json 读取代理配置给浏览器
_browser_proxy = ""
try:
    import json as _json_mod
    _cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.isfile(_cfg_path):
        with open(_cfg_path, "r") as _f:
            _cfg = _json_mod.load(_f)
        _browser_proxy = str(_cfg.get("browser_proxy", "") or _cfg.get("proxy", "") or "")
        # 读取 YesCaptcha Key（同时支持 config.json 和环境变量）
        _yescaptcha_key = str(_cfg.get("yescaptcha_key", "") or "")
        if _yescaptcha_key:
            os.environ.setdefault("YESCAPTCHA_KEY", _yescaptcha_key)
except Exception:
    pass
if _browser_proxy:
    co.set_proxy(_browser_proxy)
    print(f"[*] 浏览器代理: {_browser_proxy}")

# Linux 服务器自动检测 chromium 路径
import platform
import shutil
import glob as _glob_mod
if platform.system() == "Linux":
    # 优先用 Playwright 的 chromium（容器镜像里通常在 /ms-playwright 或 env PLAYWRIGHT_BROWSERS_PATH）
    _pw_roots = []
    _env_pw_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if _env_pw_root:
        _pw_roots.append(_env_pw_root)
    _pw_roots.append(os.path.expanduser("~/.cache/ms-playwright"))
    _pw_roots.append("/ms-playwright")

    _pw_chromes = []
    for _root in _pw_roots:
        _pw_chromes.extend(_glob_mod.glob(os.path.join(_root, "chromium-*/chrome-linux*/chrome")))

    if _pw_chromes:
        co.set_browser_path(_pw_chromes[0])
    else:
        for _candidate in ["/usr/bin/chromium-browser", "/usr/bin/chromium", "/usr/bin/google-chrome"]:
            if os.path.isfile(_candidate):
                co.set_browser_path(_candidate)
                break
    # user_data_path 在 start_browser() 每轮动态设置，此处不固定

co.set_timeouts(base=1)

# 加载修复 MouseEvent.screenX / screenY 的扩展。
EXTENSION_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "turnstilePatch"))
co.add_extension(EXTENSION_PATH)

_chrome_temp_dir: str = ""
browser = None
page = None

SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

_sso_dir = os.path.join(os.path.dirname(__file__), "sso")
_sso_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
DEFAULT_SSO_FILE = os.path.join(_sso_dir, f"sso_{_sso_ts}.txt")


def start_browser():
    # 每轮从全新浏览器开始，使用独立临时 profile 目录避免 Cookie/Session 复用。
    global browser, page, _chrome_temp_dir
    _chrome_temp_dir = tempfile.mkdtemp(prefix="chrome_run_")
    co.set_user_data_path(_chrome_temp_dir)
    browser = Chromium(co)
    tabs = browser.get_tabs()
    page = tabs[-1] if tabs else browser.new_tab()
    return browser, page


def stop_browser():
    # 完整关闭整个浏览器实例，并清理本轮临时 profile，供下一轮重新拉起。
    global browser, page, _chrome_temp_dir
    if browser is not None:
        try:
            browser.quit()
        except Exception:
            pass
    browser = None
    page = None
    if _chrome_temp_dir and os.path.isdir(_chrome_temp_dir):
        shutil.rmtree(_chrome_temp_dir, ignore_errors=True)
    _chrome_temp_dir = ""


def restart_browser():
    # 清除 cookie/storage 代替完整重启，节省 Chrome 冷启动时间。
    global browser, page
    if browser is None:
        start_browser()
        return
    try:
        tabs = browser.get_tabs()
        page = tabs[-1] if tabs else browser.new_tab()
        page.run_js("window.localStorage.clear(); window.sessionStorage.clear();")
        page.clear_cache(session_storage=True, cookies=True)
    except Exception:
        stop_browser()
        start_browser()


def refresh_active_page():
    # 验证码确认后页面会跳转，旧 page 句柄可能断开，这里统一重新获取当前活动标签页。
    global browser, page
    if browser is None:
        start_browser()
    try:
        tabs = browser.get_tabs()
        if tabs:
            page = tabs[-1]
        else:
            page = browser.new_tab()
    except Exception:
        restart_browser()
    return page


def open_signup_page():
    # 每轮开始时打开注册页，并切到“使用邮箱注册”流程。
    global page
    refresh_active_page()
    try:
        page.get(SIGNUP_URL)
    except Exception:
        refresh_active_page()
        page = browser.new_tab(SIGNUP_URL)
    click_email_signup_button()


def close_current_page():
    # 兼容旧调用名，实际行为改为整轮重启浏览器。
    restart_browser()


def has_profile_form():
    # 最终注册页只要出现姓名和密码输入框，就认为已经成功进入资料填写阶段。
    refresh_active_page()
    try:
        return bool(page.run_js(
            """
const givenInput = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
return !!(givenInput && familyInput && passwordInput);
            """
        ))
    except Exception:
        return False


def click_email_signup_button(timeout=10):
    # 页面打开后，自动点击“使用邮箱注册”按钮。
    deadline = time.time() + timeout
    while time.time() < deadline:
        clicked = page.run_js(r"""
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = candidates.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return text.includes('使用邮箱注册') || text.includes('signupwithemail') || text.includes('signupemail') || text.includes('continuewith email') || text.includes('email');
});

if (!target) {
    return false;
}

target.click();
return true;
        """)

        if clicked:
            return True

        time.sleep(0.5)

    raise Exception('未找到“使用邮箱注册”按钮')


def fill_email_and_submit(timeout=15):
    # 复用 `email_register.py` 里的邮箱获取逻辑，保留邮箱与 token 供后续验证码步骤继续使用。
    email, dev_token = get_email_and_token()
    if not email or not dev_token:
        raise Exception("获取邮箱失败")

    deadline = time.time() + timeout
    while time.time() < deadline:
        filled = page.run_js(
            """
const email = arguments[0];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;

if (!input) {
    return 'not-ready';
}

input.focus();
input.click();

// 不能只写 `input.value = xxx`，否则 React / 受控表单可能没有同步内部状态。
const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) {
    tracker.setValue('');
}
if (valueSetter) {
    valueSetter.call(input, email);
} else {
    input.value = email;
}

input.dispatchEvent(new InputEvent('beforeinput', {
    bubbles: true,
    data: email,
    inputType: 'insertText',
}));
input.dispatchEvent(new InputEvent('input', {
    bubbles: true,
    data: email,
    inputType: 'insertText',
}));
input.dispatchEvent(new Event('change', { bubbles: true }));

if ((input.value || '').trim() !== email || !input.checkValidity()) {
    return false;
}

input.blur();
return 'filled';
            """,
            email,
        )

        if filled == 'not-ready':
            time.sleep(0.5)
            continue

        if filled != 'filled':
            print(f"[Debug] 邮箱输入框已出现，但写入失败: {filled}")
            time.sleep(0.5)
            continue

        if filled == 'filled':
            time.sleep(0.8)
            clicked = page.run_js(
                r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;

if (!input || !input.checkValidity() || !(input.value || '').trim()) {
    return false;
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const t = text.toLowerCase(); return text === '注册' || text.includes('注册') || t === 'signup' || t === 'sign up' || t.includes('sign up');
});

if (!submitButton || submitButton.disabled) {
    return false;
}

submitButton.click();
return true;
                """
            )

            if clicked:
                print(f"[*] 已填写邮箱并点击注册: {email}")
                return email, dev_token

        time.sleep(0.5)

    raise Exception("未找到邮箱输入框或注册按钮")



def fill_code_and_submit(email, dev_token, timeout=60):
    # 复用 `email_register.py` 里的验证码轮询逻辑，等待邮件到达后自动填写 OTP。
    code = get_oai_code(dev_token, email)
    if not code:
        raise Exception("获取验证码失败")

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            filled = page.run_js(
                """
const code = String(arguments[0] || '').trim();

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setNativeValue(input, value) {
    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) {
        tracker.setValue('');
    }
    if (nativeInputValueSetter) {
        nativeInputValueSetter.call(input, '');
        nativeInputValueSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }
}

function dispatchInputEvents(input, value) {
    input.dispatchEvent(new InputEvent('beforeinput', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const input = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || code.length || 6) > 1;
}) || null;

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) {
        return false;
    }
    const maxLength = Number(node.maxLength || 0);
    const autocomplete = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || autocomplete === 'one-time-code';
});

if (!input && otpBoxes.length < code.length) {
    return 'not-ready';
}

if (input) {
    input.focus();
    input.click();
    setNativeValue(input, code);
    dispatchInputEvents(input, code);

    const normalizedValue = String(input.value || '').trim();
    const expectedLength = Number(input.maxLength || code.length || 6);
    const slots = Array.from(document.querySelectorAll('[data-input-otp-slot="true"]'));
    const filledSlots = slots.filter((slot) => (slot.textContent || '').trim()).length;

    if (normalizedValue !== code) {
        return 'aggregate-mismatch';
    }

    if (expectedLength > 0 && normalizedValue.length !== expectedLength) {
        return 'aggregate-length-mismatch';
    }

    if (slots.length && filledSlots && filledSlots !== normalizedValue.length) {
        return 'aggregate-slot-mismatch';
    }

    input.blur();
    return 'filled';
}

const orderedBoxes = otpBoxes.slice(0, code.length);
for (let i = 0; i < orderedBoxes.length; i += 1) {
    const box = orderedBoxes[i];
    const char = code[i] || '';
    box.focus();
    box.click();
    setNativeValue(box, char);
    dispatchInputEvents(box, char);
    box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: char }));
    box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: char }));
    box.blur();
}

const merged = orderedBoxes.map((node) => String(node.value || '').trim()).join('');
return merged === code ? 'filled' : 'box-mismatch';
                """,
                code,
            )
        except PageDisconnectedError:
            # 点击确认邮箱后如果刚好发生跳转，旧页面句柄会断开；此时切到新页继续判断即可。
            refresh_active_page()
            if has_profile_form():
                print("[*] 验证码提交后已跳转到最终注册页。")
                return code
            time.sleep(1)
            continue

        if filled == 'not-ready':
            if has_profile_form():
                print("[*] 已直接进入最终注册页，跳过验证码按钮确认。")
                return code
            time.sleep(0.5)
            continue

        if filled != 'filled':
            print(f"[Debug] 验证码输入框已出现，但写入失败: {filled}")
            time.sleep(0.5)
            continue

        if filled == 'filled':
            time.sleep(1.2)
            try:
                clicked = page.run_js(
                    r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const aggregateInput = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 0) > 1;
}) || null;

let value = '';
if (aggregateInput) {
    value = String(aggregateInput.value || '').trim();
    const expectedLength = Number(aggregateInput.maxLength || value.length || 6);
    if (!value || (expectedLength > 0 && value.length !== expectedLength)) {
        return false;
    }

    const slots = Array.from(document.querySelectorAll('[data-input-otp-slot="true"]'));
    if (slots.length) {
        const filledSlots = slots.filter((slot) => (slot.textContent || '').trim()).length;
        if (filledSlots && filledSlots !== value.length) {
            return false;
        }
    }
} else {
    const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
        if (!isVisible(node) || node.disabled || node.readOnly) {
            return false;
        }
        const maxLength = Number(node.maxLength || 0);
        const autocomplete = String(node.autocomplete || '').toLowerCase();
        return maxLength === 1 || autocomplete === 'one-time-code';
    });
    value = otpBoxes.map((node) => String(node.value || '').trim()).join('');
    if (!value || value.length < 6) {
        return false;
    }
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const confirmButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const t = text.toLowerCase(); return text === '确认邮箱' || text.includes('确认邮箱') || text === '继续' || text.includes('继续') || text === '下一步' || text.includes('下一步') || t.includes('confirm') || t.includes('continue') || t.includes('next') || t.includes('verify');
});

if (!confirmButton) {
    return 'no-button';
}

confirmButton.focus();
confirmButton.click();
return 'clicked';
                    """
                )
            except PageDisconnectedError:
                refresh_active_page()
                if has_profile_form():
                    print("[*] 确认邮箱后页面跳转成功，已进入最终注册页。")
                    return code
                clicked = 'disconnected'

            if clicked == 'clicked':
                print(f"[*] 已填写验证码并点击确认邮箱: {code}")
                time.sleep(2)
                refresh_active_page()
                if has_profile_form():
                    print("[*] 验证码确认完成，最终注册页已就绪。")
                return code

            if clicked == 'no-button':
                current_url = page.url
                if 'sign-up' in current_url or 'signup' in current_url:
                    print(f"[*] 已填写验证码，页面已自动跳转到下一步: {current_url}")
                    return code

            if clicked == 'disconnected':
                time.sleep(1)
                continue

        time.sleep(0.5)

    debug_snapshot = page.run_js(
        r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const inputs = Array.from(document.querySelectorAll('input')).filter(isVisible).map((node) => ({
    type: node.type || '',
    name: node.name || '',
    testid: node.getAttribute('data-testid') || '',
    autocomplete: node.autocomplete || '',
    maxLength: Number(node.maxLength || 0),
    value: String(node.value || ''),
}));

const buttons = Array.from(document.querySelectorAll('button')).filter(isVisible).map((node) => ({
    text: String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim(),
    disabled: !!node.disabled,
    ariaDisabled: node.getAttribute('aria-disabled') || '',
}));

return { url: location.href, inputs, buttons };
        """
    )
    print(f"[Debug] 验证码页 DOM 摘要: {debug_snapshot}")
    raise Exception("未找到验证码输入框或确认邮箱按钮")


def getTurnstileToken(action=None, data=None):
    """
    通过 YesCaptcha API 解决 Turnstile 验证，无需浏览器点击。
    Sitekey: 0x4AAAAAAAhr9JGVDZbrZOo0
    Site:    https://accounts.x.ai
    """
    SITE_URL = "https://accounts.x.ai"
    SITE_KEY = "0x4AAAAAAAhr9JGVDZbrZOo0"
    
    # 获取保底 Action 和 Data (参考 grok.py)
    # Action 通常为 'signup'
    # Data 通常为 Next.js 的 state tree
    final_action = action or "signup"
    final_data = data or "%5B%22%22%2C%7B%22children%22%3A%5B%22(app)%22%2C%7B%22children%22%3A%5B%22(auth)%22%2C%7B%22children%22%3A%5B%22sign-up%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2C%22%2Fsign-up%22%2C%22refresh%22%5D%7D%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%2Ctrue%5D"

    print(f"[Turnstile] 正在通过 YesCaptcha 解决验证 (action={final_action})...")
    try:
        service = TurnstileService()
        token = service.solve(SITE_URL, SITE_KEY, action=final_action, data=final_data)
        if token:
            return token
        raise Exception("YesCaptcha 返回了空 Token")
    except Exception as e:
        print(f"[Turnstile] YesCaptcha 失败: {e}")
        raise Exception(f"Turnstile 验证失败: {e}")








def build_profile():
    # 生成一组可重复使用的注册资料，密码至少包含大小写、数字和特殊字符。
    given_name = "Neo"
    family_name = "Lin"
    password = "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
    return given_name, family_name, password


def fill_profile_and_submit(timeout=30):
    # 在验证码通过后，直接锁定“可见且可写”的真实输入框，避免命中隐藏节点或 React 受控副本。
    given_name, family_name, password = build_profile()
    deadline = time.time() + timeout
    turnstile_token = ""

def fill_profile_and_submit(timeout=45):
    # 使用原生输入模拟替代 JS 暴力设置，确保 React/Next.js 状态同步。
    given_name, family_name, password = build_profile()
    deadline = time.time() + timeout
    turnstile_token = ""

    print(f"[*] 开始填写注册资料 (超时: {timeout}s)...")
    while time.time() < deadline:
        # 1. 定位输入框
        try:
            given_el = page.ele('css:input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]', timeout=3)
            family_el = page.ele('css:input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]', timeout=2)
            pass_el = page.ele('css:input[data-testid="password"], input[name="password"], input[type="password"]', timeout=2)
            
            if not (given_el and family_el and pass_el):
                print("[Debug] 正在等待注册输入框出现...")
                time.sleep(1)
                continue
            
            # 2. 模拟真实输入
            print(f"[*] 模拟输入: {given_name} {family_name}")
            given_el.clear(); given_el.input(given_name)
            family_el.clear(); family_el.input(family_name)
            pass_el.clear(); pass_el.input(password)
            
            # 3. 校验输入内容是否持久化
            if not (given_el.value == given_name and family_el.value == family_name):
                print("[Warn] 字段值校验不通过，尝试重新模拟输入...")
                time.sleep(0.5)
                continue
                
            # 4. 处理 Turnstile (如果存在)
            turnstile_state = page.run_js(
                """
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!challengeInput) return 'not-found';
return String(challengeInput.value || '').trim() ? 'ready' : 'pending';
                """
            )

            if turnstile_state == "pending" and not turnstile_token:
                print("[*] 检测到 Turnstile 尚未完成，正在申请解决...")
                # 尝试从页面获取 action (通常是 signup)
                page_action = page.run_js("return window.__cf_chl_opt?.action || 'signup'")
                turnstile_token = getTurnstileToken(action=page_action)
                if turnstile_token:
                    page.run_js(
                        """
const token = arguments[0];
const inp = document.querySelector('input[name="cf-turnstile-response"]');
if (inp) {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    if (setter) setter.call(inp, token); else inp.value = token;
    inp.dispatchEvent(new Event('input', { bubbles: true }));
    inp.dispatchEvent(new Event('change', { bubbles: true }));
}
// 强制覆盖 Turnstile API 返回值，防止 JS 校验失败
if (window.turnstile) {
    const originalGetResponse = window.turnstile.getResponse;
    window.turnstile.getResponse = function() { return token; };
}
// 尝试触发可能的 Success Callback (启发式搜索)
try {
    const callbackName = window.__cf_chl_opt?.c || window.turnstile_callback;
    if (callbackName && typeof window[callbackName] === 'function') {
        window[callbackName](token);
    }
} catch(e) {}
                        """,
                        turnstile_token
                    )
                    print("[*] Turnstile 令牌已注入并覆盖 API。")

            # 5. 寻找并点击提交按钮
            btn = (page.ele('tag:button@@text()=完成注册') or 
                   page.ele('tag:button@@text():Create Account') or 
                   page.ele('tag:button@@text():Sign up') or
                   page.ele('css:button[type="submit"]'))
            
            if btn and not btn.attr('disabled'):
                btn.click()
                print(f"[*] 已点击完成注册: {given_name} {family_name} / {password}")
                
                # ── 增加即时快照：点击后 2s 存现场 ──
                time.sleep(2)
                try:
                    log_dir = os.path.join(os.path.dirname(__file__), "logs")
                    os.makedirs(log_dir, exist_ok=True)
                    page.get_screenshot(path=os.path.join(log_dir, "post_click_stage.png"), full_page=True)
                    print("[Debug] 已保存点击后实时快照: logs/post_click_stage.png")
                except: pass
                
                return {
                    "given_name": given_name,
                    "family_name": family_name,
                    "password": password,
                }
            else:
                print("[Debug] 注册按钮尚未处于可点击状态...")

        except Exception as e:
            print(f"[Debug] 填写表单异常: {e}")

        time.sleep(1.5)

    raise Exception("未能在超时时间内完成资料填写或提交")


def extract_visible_numbers(timeout=60):
    # 登录/注册完成后，提取页面上可见的普通数字文本，不处理任何敏感 Cookie。
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = page.run_js(
            r"""
function isVisible(el) {
    if (!el) {
        return false;
    }
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const selector = [
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'div', 'span', 'p', 'strong', 'b', 'small',
    '[data-testid]', '[class]', '[role="heading"]'
].join(',');

const seen = new Set();
const matches = [];
for (const node of document.querySelectorAll(selector)) {
    if (!isVisible(node)) {
        continue;
    }
    const text = String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
    if (!text) {
        continue;
    }
    const found = text.match(/\d+(?:\.\d+)?/g);
    if (!found) {
        continue;
    }
    for (const value of found) {
        const key = `${value}@@${text}`;
        if (seen.has(key)) {
            continue;
        }
        seen.add(key);
        matches.push({ value, text });
    }
}

return matches.slice(0, 30);
            """
        )

        if result:
            print("[*] 页面可见数字文本提取结果:")
            for item in result:
                try:
                    print(f"    - 数字: {item['value']} | 上下文: {item['text']}")
                except Exception:
                    pass
            return result

        time.sleep(1)

    raise Exception("登录后未提取到可见数字文本")


def wait_for_sso_cookie(timeout=60):
    # 必须在注册完成后再取 sso，优先抓取精确的 sso cookie。
    # 增强版：增加截图和 HTML 导出，便于排查 sso 为空的原因。
    deadline = time.time() + timeout
    last_seen_names = set()
    start_url = page.url

    print(f"[*] 正在等待 sso cookie (超时: {timeout}s)...")
    while time.time() < deadline:
        try:
            refresh_active_page()
            if page is None:
                time.sleep(1)
                continue

            # 诊断：当前的 URL 和标题
            curr_url = page.url
            curr_title = page.title
            if curr_url != start_url:
                print(f"[Debug] URL 已变化: {curr_url} | 标题: {curr_title}")

            cookies = page.cookies(all_domains=True, all_info=True) or []
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                else:
                    name = str(getattr(item, "name", "")).strip()
                    value = str(getattr(item, "value", "")).strip()

                if name:
                    last_seen_names.add(name)

                if name == "sso" and value:
                    print("[*] 注册完成后已获取到 sso cookie。")
                    return value
            
            # 如果 URL 变成了 login 或者 error，可能已经失败
            if "login" in curr_url.lower() or "error" in curr_url.lower():
                print(f"[Warn] 检测到 URL 异常（可能跳转到了登录或错误页）: {curr_url}")

        except PageDisconnectedError:
            refresh_active_page()
        except Exception as e:
            print(f"[Debug] 轮询 Cookie 异常: {e}")

        time.sleep(2)

    # ── 超时诊断：保存现场 ──
    try:
        log_dir = os.path.join(os.path.dirname(__file__), "logs")
        os.makedirs(log_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%H%M%S")
        
        # 1. 截图
        shot_path = os.path.join(log_dir, f"sso_timeout_{ts}.png")
        page.get_screenshot(path=shot_path, full_page=True)
        print(f"[Turnstile] 超时已保存截图: {shot_path}")
        
        # 2. HTML 源码
        html_path = os.path.join(log_dir, f"sso_timeout_{ts}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(page.html)
        print(f"[Turnstile] 超时已保存 HTML: {html_path}")
    except Exception as diag_err:
        print(f"[Error] 保存故障快照失败: {diag_err}")

    raise Exception(f"注册完成后未获取到 sso cookie，当前已见 cookie: {sorted(last_seen_names)}")


def append_sso_to_txt(sso_value, output_path=DEFAULT_SSO_FILE):
    # 按用户要求，一行写一个 sso 值，持续追加。
    normalized = str(sso_value or "").strip()
    if not normalized:
        raise Exception("待写入的 sso 为空")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "a", encoding="utf-8") as file:
        file.write(normalized + "\n")

    print(f"[*] 已追加写入 sso 到文件: {output_path}")


def push_sso_to_api(new_tokens: list):
    # 推送 SSO token 到 grok2api 管理接口。
    # append=false：直接将本次 token 列表全量推送（覆盖）。
    # append=true（默认）：先 GET 查询线上现有 token，合并本次后全量推送。
    import json
    import urllib3
    import requests
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            conf = json.load(f)
    except Exception as e:
        print(f"[Warn] 读取 config.json 失败，跳过推送: {e}")
        return

    api_conf = conf.get("api", {})
    endpoint = str(api_conf.get("endpoint", "")).strip()
    api_token = str(api_conf.get("token", "")).strip()
    append_mode = api_conf.get("append", True)

    if not endpoint or not api_token:
        return

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    tokens_to_push = [t for t in new_tokens if t]

    if append_mode:
        try:
            get_resp = requests.get(endpoint, headers=headers, timeout=15, verify=False)
            if get_resp.status_code == 200:
                data = get_resp.json()
                # 兼容两种响应格式：
                # 新版: {"tokens": {"ssoBasic": [...]}}
                # 旧版: {"ssoBasic": [...]}
                if isinstance(data, dict) and isinstance(data.get("tokens"), dict):
                    existing = data["tokens"].get("ssoBasic", [])
                else:
                    existing = data.get("ssoBasic", []) if isinstance(data, dict) else []
                existing_tokens = [
                    item["token"] if isinstance(item, dict) else str(item)
                    for item in existing if item
                ]
                seen = set()
                deduped = []
                for t in existing_tokens + tokens_to_push:
                    if t not in seen:
                        seen.add(t)
                        deduped.append(t)
                tokens_to_push = deduped
                print(f"[*] 查询到线上 {len(existing_tokens)} 个 token，合并本次 {len(new_tokens)} 个，共 {len(deduped)} 个")
            else:
                print(f"[Warn] 查询线上 token 失败: HTTP {get_resp.status_code}，仅推送本次 token")
        except Exception as e:
            print(f"[Warn] 查询线上 token 异常: {e}，仅推送本次 token")

    try:
        resp = requests.post(
            endpoint,
            json={"ssoBasic": tokens_to_push},
            headers=headers,
            timeout=60,
            verify=False,
        )
        if resp.status_code == 200:
            print(f"[*] SSO token 已推送到 API（共 {len(tokens_to_push)} 个）: {endpoint}")
        else:
            print(f"[Warn] 推送 API 返回异常: HTTP {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[Warn] 推送 API 失败: {e}")


def fill_profile_and_submit(verify_code, email):
    # 最终注册页：填写姓名、密码。
    # 增强版：改用 JS fetch 模拟 grok.py 的注册逻辑，绕过 UI 提交限制。
    print(f"[*] 正在为 {email} 准备注册资料...")
    
    # 1. 生成随机姓名和密码 (与 grok.py 逻辑一致)
    first_name = "Neo"
    last_name = "Lin"
    password = f"N{secrets.token_hex(4)}!a7#-w-{secrets.token_hex(2)}"
    
    # ... (rest of the function remains the same, but using the passed 'email')
    # ... (I'll just replace the relevant lines for brevity in this thought, but write the full function here)
    try:
        page.ele('css:input[name="first_name"], input[placeholder*="First"], input[autocomplete="given-name"]').input(first_name)
        page.ele('css:input[name="last_name"], input[placeholder*="Last"], input[autocomplete="family-name"]').input(last_name)
        page.ele('css:input[name="password"], input[type="password"]').input(password)
    except Exception as e:
        print(f"[Debug] 填充表单辅助失败 (不影响后续 fetch): {e}")

    # 3. 获取 Turnstile Token
    turnstile_token = None
    for _ in range(3):
        page_action = page.run_js("return window.__cf_chl_opt?.action || 'signup'")
        turnstile_token = getTurnstileToken(action=page_action)
        if turnstile_token:
            break
        time.sleep(2)
        
    if not turnstile_token:
        raise Exception("无法获取有效的 Turnstile Token")

    # 4. 扫描 Action ID
    print("[*] 正在扫描 Next.js Action ID...")
    action_id = page.run_js("""
        return document.documentElement.innerHTML.match(/7f[a-fA-F0-9]{40}/)?.[0] || null;
    """)
    if not action_id:
        js_urls = page.run_js('return Array.from(document.querySelectorAll(\'script[src*="/_next/static/chunks/"]\')).map(s => s.src)')
        for url in js_urls:
            try:
                content = page.get(url, show_errmsg=False).text
                match = re.search(r'7f[a-fA-F0-9]{40}', content)
                if match:
                    action_id = match.group(0)
                    break
            except Exception: pass

    # 5. 执行 Hybrid 注册 (JS Fetch)
    print(f"[*] 正在通过浏览器环境模拟 API 注册: {email}")
    
    # 防止 action_id 为 None 导致 run_js 报错
    final_action_id = action_id or ""
    
    state_tree = "%5B%22%22%2C%7B%22children%22%3A%5B%22(app)%22%2C%7B%22children%22%3A%5B%22(auth)%22%2C%7B%22children%22%3A%5B%22sign-up%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2C%22%2Fsign-up%22%2C%22refresh%22%5D%7D%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%2Ctrue%5D"
    
    fetch_js = """
    const payload = [{
        "emailValidationCode": arguments[0],
        "createUserAndSessionRequest": {
            "email": arguments[1], "givenName": arguments[2], "familyName": arguments[3],
            "clearTextPassword": arguments[4], "tosAcceptedVersion": "$undefined"
        },
        "turnstileToken": arguments[5], "promptOnDuplicateEmail": true
    }];
    
    const headers = {
        "accept": "text/x-component",
        "content-type": "text/plain;charset=UTF-8",
        "next-router-state-tree": arguments[6]
    };
    if (arguments[7]) headers["next-action"] = arguments[7];

    return fetch("/sign-up", {
        method: "POST",
        headers: headers,
        body: JSON.stringify(payload)
    }).then(r => r.text()).catch(e => "FETCH_ERROR: " + e.message);
    """
    
    response_text = page.run_js(fetch_js, verify_code, email, first_name, last_name, password, turnstile_token, state_tree, final_action_id)
    
    if "FETCH_ERROR" in response_text:
        raise Exception(f"注册 Fetch 失败: {response_text}")

    match = re.search(r'(https://[^" \s]+set-cookie\?q=[^:" \s]+)1:', response_text)
    if match:
        verify_url = match.group(1)
        print(f"[*] 成功提取到 Set-Cookie URL: {verify_url}")
        page.get(verify_url)
    else:
        print(f"[Debug] 注册响应体未见 Set-Cookie URL: {response_text[:200]}")
        try:
             btn = page.ele('css:button[type="submit"]')
             if btn: btn.click()
        except Exception: pass

    return {"password": password, "given_name": first_name, "family_name": last_name}


def run_single_registration(output_path=DEFAULT_SSO_FILE, extract_numbers=False):
    # 单轮流程：打开注册页 -> 完成注册 -> 获取 sso -> 写 txt。
    open_signup_page()
    email, dev_token = fill_email_and_submit()
    verify_code = fill_code_and_submit(email, dev_token)
    profile = fill_profile_and_submit(verify_code, email)
    sso_value = wait_for_sso_cookie()
    append_sso_to_txt(sso_value, output_path)

    if extract_numbers:
        extract_visible_numbers()

    result = {
        "email": email,
        "sso": sso_value,
        **profile,
    }

    if run_logger:
        run_logger.info(
            "注册成功 | email=%s | password=%s | given=%s | family=%s",
            email,
            profile.get("password", ""),
            profile.get("given_name", ""),
            profile.get("family_name", ""),
        )

    print(f"[*] 本轮注册完成，邮箱: {email}")
    return result


def load_run_count() -> int:
    # 从 config.json 读取默认执行轮数，配置不存在时返回 10。
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        import json
        with open(config_path, "r", encoding="utf-8") as f:
            conf = json.load(f)
        v = conf.get("run", {}).get("count")
        if isinstance(v, int) and v >= 0:
            return v
    except Exception:
        pass
    return 10


def main():
    # 默认循环执行；每轮完成后关闭当前页，再自动进入下一轮。
    global run_logger
    run_logger = setup_run_logger()

    config_count = load_run_count()

    parser = argparse.ArgumentParser(description="xAI 自动注册并采集 sso")
    parser.add_argument("--count", type=int, default=config_count, help=f"执行轮数，0 表示无限循环（默认读取 config.json run.count，当前 {config_count}）")
    parser.add_argument("--output", default=DEFAULT_SSO_FILE, help="sso 输出 txt 路径")
    parser.add_argument("--extract-numbers", action="store_true", help="注册完成后额外提取页面数字文本")
    args = parser.parse_args()

    current_round = 0
    collected_sso: list = []
    try:
        start_browser()
        while True:
            if args.count > 0 and current_round >= args.count:
                break

            current_round += 1
            print(f"\n[*] 开始第 {current_round} 轮注册")
            round_succeeded = False

            try:
                result = run_single_registration(args.output, extract_numbers=args.extract_numbers)
                collected_sso.append(result["sso"])
                round_succeeded = True
            except KeyboardInterrupt:
                print("\n[Info] 收到中断信号，停止后续轮次。")
                break
            except Exception as error:
                print(f"[Error] 第 {current_round} 轮失败: {error}")
            finally:
                restart_browser()

            if args.count == 0 or current_round < args.count:
                time.sleep(2)

    finally:
        if collected_sso:
            print(f"\n[*] 注册完成，推送 {len(collected_sso)} 个 token 到 API...")
            push_sso_to_api(collected_sso)

        stop_browser()


if __name__ == "__main__":
    main()
