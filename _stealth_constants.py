"""
_stealth_constants.py
=====================
Single source of truth for all Cloudflare / bot-detection evasion settings.
Import this in discovery.py, extraction.py, and install.py.

What each layer defeats:
  STEALTH_UA          — HTTP-level User-Agent check
  EXTRA_HEADERS       — TLS/HTTP header fingerprint (Sec-CH-UA, Sec-Fetch-*, Accept)
  LAUNCH_ARGS         — Chrome flag-based detection (AutomationControlled, etc.)
  STEALTH_JS          — JS-runtime fingerprinting (webdriver, WebGL, canvas, plugins,
                        permissions, screen, outerWidth, timing, CDP globals)
  random_human_delay  — Timing-entropy bot detection
  human_mouse_move    — Mouse-movement entropy detection
"""

import random
import asyncio

# ---------------------------------------------------------------------------
# 1. USER-AGENT  — keep in sync with LAUNCH_ARGS --user-agent and STEALTH_JS
#    Use a recent stable Chrome on Windows (the most common desktop profile).
# ---------------------------------------------------------------------------
STEALTH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# 2. HTTP HEADERS  — Cloudflare inspects these at the TLS/HTTP layer.
#    Sec-CH-UA must match the UA version. Sec-Fetch-* must be present.
#    Missing or inconsistent headers are a primary bot signal.
# ---------------------------------------------------------------------------
EXTRA_HEADERS = {
    "User-Agent":                STEALTH_UA,
    "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language":           "en-US,en;q=0.9",
    "Accept-Encoding":           "gzip, deflate, br",
    "Sec-CH-UA":                 '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-CH-UA-Mobile":          "?0",
    "Sec-CH-UA-Platform":        '"Windows"',
    "Sec-Fetch-Dest":            "document",
    "Sec-Fetch-Mode":            "navigate",
    "Sec-Fetch-Site":            "none",
    "Sec-Fetch-User":            "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection":                "keep-alive",
    "DNT":                       "1",
}

# Same headers for requests.get() calls (subset that HTTP libs use)
REQUESTS_HEADERS = {
    "User-Agent":      STEALTH_UA,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-CH-UA":       '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-CH-UA-Mobile":"?0",
    "Sec-Fetch-Dest":  "document",
    "Sec-Fetch-Mode":  "navigate",
    "Sec-Fetch-Site":  "none",
    "Connection":      "keep-alive",
}

# ---------------------------------------------------------------------------
# 3. LAUNCH ARGS  — Chrome flags that leak automation identity
# ---------------------------------------------------------------------------
LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    # ── Core automation flag removal ─────────────────────────────────────────
    "--disable-blink-features=AutomationControlled",
    "--ignore-certificate-errors",
    "--disable-web-security",
    "--lang=en-US,en",
    f"--user-agent={STEALTH_UA}",
    # ── Prevent isolation fingerprinting ────────────────────────────────────
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-site-isolation-trials",
    "--disable-features=BlockInsecurePrivateNetworkRequests",
    # ── Look like a normal Chrome session ───────────────────────────────────
    "--metrics-recording-only",
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    "--use-mock-keychain",
    # ── Viewport consistency (matches STEALTH_JS outerWidth/screen) ─────────
    "--window-size=1366,768",
    # ── Prevent GPU process detection ───────────────────────────────────────
    "--disable-gpu-sandbox",
    # ── Reduce timing side-channels ─────────────────────────────────────────
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
]

# ---------------------------------------------------------------------------
# 4. STEALTH JS  — Injected via add_init_script() before every page load.
#    Defeats JS-runtime fingerprinting. Covers:
#      • navigator.webdriver / plugins / languages / platform / vendor
#      • hardwareConcurrency / deviceMemory
#      • window.chrome runtime object
#      • CDP/Playwright global removal
#      • WebGL vendor & renderer strings (SwiftShader → Intel)
#      • Canvas fingerprint noise (subtle pixel perturbation)
#      • Permissions API (notifications → 'default' not 'denied')
#      • navigator.connection (network type)
#      • screen / outerWidth / outerHeight (match window-size flag)
#      • Date/timing jitter to defeat timing-based bot detection
# ---------------------------------------------------------------------------
STEALTH_JS = r"""
// ── 1. Core webdriver removal ─────────────────────────────────────────────
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

// ── 2. Language / plugin fingerprint ─────────────────────────────────────
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
// Fake realistic plugin objects (not just [1,2,3])
const fakePlugins = [
  {name:'Chrome PDF Plugin',       filename:'internal-pdf-viewer', description:'Portable Document Format', length:1},
  {name:'Chrome PDF Viewer',       filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai', description:'', length:1},
  {name:'Native Client',           filename:'internal-nacl-plugin', description:'', length:2},
];
Object.defineProperty(navigator, 'plugins', {
  get: () => Object.assign(fakePlugins, {item: i => fakePlugins[i], namedItem: n => fakePlugins.find(p=>p.name===n), refresh: ()=>{}}),
});
Object.defineProperty(navigator, 'mimeTypes', {
  get: () => [{type:'application/pdf', suffixes:'pdf', description:'', enabledPlugin: fakePlugins[0]}],
});

// ── 3. Hardware fingerprint ───────────────────────────────────────────────
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory',        {get: () => 8});

// ── 4. Platform consistency ───────────────────────────────────────────────
Object.defineProperty(navigator, 'platform',   {get: () => 'Win32'});
Object.defineProperty(navigator, 'vendor',     {get: () => 'Google Inc.'});
Object.defineProperty(navigator, 'appVersion', {get: () =>
  '5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
});

// ── 5. Chrome runtime object (detectors check this exists and has shape) ──
window.chrome = {
  runtime: {
    connect: function(){},
    sendMessage: function(){},
    onMessage: {addListener: function(){}, removeListener: function(){}},
    id: undefined,
  },
  loadTimes: function() {},
  csi: function() {},
  app: {isInstalled: false, getDetails: function(){}, getIsInstalled: function(){}, installState: function(){}, runningState: function(){}},
};

// ── 6. Remove CDP / Playwright / Selenium globals ─────────────────────────
(function() {
  const killKeys = [
    '__playwright', '__pw_manual', '__PW_inspect',
    '_selenium', '__webdriver_script_fn', '__driver_evaluate',
    '__webdriver_evaluate', '__selenium_evaluate',
    'cdc_adoQpoasnfa76pfcZLmcfl_Array',
    'cdc_adoQpoasnfa76pfcZLmcfl_Promise',
    'cdc_adoQpoasnfa76pfcZLmcfl_Symbol',
    '$chrome_asyncScriptInfo', '$cdc_asdjflasutopfhvcZLmcfl_',
  ];
  killKeys.forEach(k => { try { delete window[k]; } catch(e) {} });
  // Re-delete after any late injection
  const orig = window.onerror;
  window.onerror = function(msg, src, row, col, err) {
    killKeys.forEach(k => { try { delete window[k]; } catch(e) {} });
    return orig ? orig(msg, src, row, col, err) : false;
  };
})();

// ── 7. WebGL fingerprint ── defeat "Google SwiftShader" headless signal ───
(function() {
  const getParam = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(param) {
    if (param === 37445) return 'Intel Inc.';                  // UNMASKED_VENDOR_WEBGL
    if (param === 37446) return 'Intel Iris Pro OpenGL Engine'; // UNMASKED_RENDERER_WEBGL
    return getParam.call(this, param);
  };
  try {
    const getParam2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(param) {
      if (param === 37445) return 'Intel Inc.';
      if (param === 37446) return 'Intel Iris Pro OpenGL Engine';
      return getParam2.call(this, param);
    };
  } catch(e) {}
})();

// ── 8. Canvas fingerprint noise ───────────────────────────────────────────
// Adds subtle imperceptible per-session noise so canvas hash never matches
// the known headless Chrome hash.
(function() {
  const noise = Math.random() * 0.01;
  const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = function(type) {
    const ctx = this.getContext('2d');
    if (ctx) {
      const imgData = ctx.getImageData(0, 0, this.width || 1, this.height || 1);
      for (let i = 0; i < imgData.data.length; i += 4) {
        imgData.data[i]     = Math.min(255, imgData.data[i]     + noise);
        imgData.data[i + 1] = Math.min(255, imgData.data[i + 1] + noise);
      }
      ctx.putImageData(imgData, 0, 0);
    }
    return origToDataURL.apply(this, arguments);
  };
  const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
  CanvasRenderingContext2D.prototype.getImageData = function(x, y, w, h) {
    const data = origGetImageData.call(this, x, y, w, h);
    for (let i = 0; i < data.data.length; i += 4) {
      data.data[i]     = Math.min(255, data.data[i]     + (Math.random() < 0.05 ? 1 : 0));
      data.data[i + 1] = Math.min(255, data.data[i + 1] + (Math.random() < 0.05 ? 1 : 0));
    }
    return data;
  };
})();

// ── 9. Permissions API ─────────────────────────────────────────────────────
// Headless returns 'denied' for notifications — real browsers say 'default'
(function() {
  if (window.Permissions && window.Permissions.prototype && window.Permissions.prototype.query) {
    const origQuery = window.Permissions.prototype.query;
    window.Permissions.prototype.query = function(params) {
      if (params && params.name === 'notifications') {
        return Promise.resolve({state: 'default', onchange: null});
      }
      return origQuery.call(this, params);
    };
  }
  try {
    if (window.Notification) {
      Object.defineProperty(Notification, 'permission', {get: () => 'default'});
    }
  } catch(e) {}
})();

// ── 10. Network connection spoof ──────────────────────────────────────────
try {
  Object.defineProperty(navigator, 'connection', {
    get: () => ({
      effectiveType: '4g', rtt: 50, downlink: 10,
      saveData: false, type: 'wifi', onchange: null,
    }),
  });
} catch(e) {}

// ── 11. Screen / outerWidth / outerHeight ─────────────────────────────────
// In headless, outerWidth=0. Must match --window-size=1366,768
try { Object.defineProperty(window, 'outerWidth',  {get: () => 1366}); } catch(e) {}
try { Object.defineProperty(window, 'outerHeight', {get: () => 768});  } catch(e) {}
try {
  Object.defineProperty(window, 'screen', {get: () => ({
    width: 1366, height: 768, availWidth: 1366, availHeight: 728,
    colorDepth: 24, pixelDepth: 24, orientation: {type:'landscape-primary', angle:0},
  })});
} catch(e) {}

// ── 12. Audio context fingerprint ─────────────────────────────────────────
try {
  const origGetChannelData = AudioBuffer.prototype.getChannelData;
  AudioBuffer.prototype.getChannelData = function() {
    const arr = origGetChannelData.apply(this, arguments);
    for (let i = 0; i < arr.length; i += 100) {
      arr[i] += Math.random() * 0.0001;
    }
    return arr;
  };
} catch(e) {}

// ── 13. iframe contentWindow consistency ──────────────────────────────────
// Some detectors open an iframe and check if window.navigator.webdriver
// differs between parent and iframe context.
try {
  const origDesc = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
  Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
    get() {
      const win = origDesc.get.call(this);
      if (!win) return win;
      try { Object.defineProperty(win.navigator, 'webdriver', {get: () => undefined}); } catch(e) {}
      return win;
    }
  });
} catch(e) {}
"""

# ---------------------------------------------------------------------------
# 5. HUMAN TIMING HELPERS
# ---------------------------------------------------------------------------

async def random_human_delay(min_s: float = 0.8, max_s: float = 2.5) -> None:
    """Randomised pause that mimics human reading/reaction time."""
    await asyncio.sleep(random.uniform(min_s, max_s))


async def human_mouse_move(page, num_moves: int = 3) -> None:
    """
    Move the mouse in a few random arcs across the viewport.
    Cloudflare TurnstileBot and PerimeterX both analyse mouse entropy.
    """
    try:
        vp = page.viewport_size or {"width": 1366, "height": 768}
        w, h = vp["width"], vp["height"]
        for _ in range(num_moves):
            x = random.randint(100, w - 100)
            y = random.randint(100, h - 100)
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.05, 0.18))
    except Exception:
        pass


async def apply_stealth_context(context) -> None:
    """
    Call this once after creating a BrowserContext.
    Sets extra HTTP headers + injects STEALTH_JS for every new page/frame.
    """
    await context.set_extra_http_headers(EXTRA_HEADERS)
    await context.add_init_script(STEALTH_JS)


async def apply_stealth_page(page) -> None:
    """
    Call this if you create a page AFTER the context was set up,
    or need to re-apply stealth (e.g. after a new tab opens).
    """
    await page.add_init_script(STEALTH_JS)
    # Brief human-like pause before any real action
    await random_human_delay(0.3, 0.9)