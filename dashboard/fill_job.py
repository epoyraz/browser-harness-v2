"""bh script: fill ONE application and screenshot it. Never submits, never uploads a CV.

Config comes from the environment so the server owns the applicant details.
Prints one JSON line prefixed with RESULT.
"""
import contextlib
import json
import os
import time

from harness.core.outcome import HarnessError

IDX = int(os.environ["DASH_IDX"])
URL = os.environ["DASH_URL"]
SHOT = os.environ["DASH_SHOT"]
A = json.loads(os.environ.get("DASH_APPLICANT", "{}"))

FIRST = A.get("first", "")
LAST = A.get("last", "")
EMAIL = A.get("email", "")
PHONE = A.get("phone", "")
CITY = A.get("city", "Zurich")
COVER = A.get("cover", "")

res = {"idx": IDX, "filled": 0, "attempted": 0, "shot": "", "note": "", "submitted": False}


def pick(label):
    t = " ".join(str(label or "").lower().split())
    if not t:
        return None
    if any(k in t for k in ("vorname", "first name", "firstname", "given name")):
        return FIRST
    if any(k in t for k in ("nachname", "last name", "lastname", "surname", "familienname")):
        return LAST
    if any(k in t for k in ("full name", "vollständiger name", "ihr name", "your name")):
        return f"{FIRST} {LAST}".strip()
    if "mail" in t:
        return EMAIL
    if any(k in t for k in ("telefon", "phone", "mobil", "handy", "natel")):
        return PHONE
    if any(k in t for k in ("wohnort", "city", "stadt", "ort")):
        return CITY
    if any(k in t for k in ("motivation", "anschreiben", "cover", "nachricht", "message",
                            "bemerkung", "comment", "warum", "why")):
        return COVER
    if t.strip() in ("name", "name *", "name*"):
        return f"{FIRST} {LAST}".strip()
    return None


try:
    new_tab(URL)
    time.sleep(2.8)

    # consent overlays render inside shadow roots, where querySelectorAll cannot reach
    js(r"""
    (() => {const WANT=['accept all','accept all cookies','alle akzeptieren',
      'alle cookies akzeptieren','akzeptieren','zustimmen','einverstanden','allow all',
      'i agree','accept cookies','verstanden','tout accepter','accept'];
     const scan=(root,d)=>{ if(!root||d>6) return false;
       let els; try{els=root.querySelectorAll('button,[role=button],a');}catch(e){return false;}
       for(const el of els){const t=(el.innerText||el.textContent||'').trim().toLowerCase();
         if(t&&t.length<40&&WANT.includes(t)){try{el.click();return true;}catch(e){}}}
       try{for(const el of root.querySelectorAll('*')) if(el.shadowRoot&&scan(el.shadowRoot,d+1)) return true;}catch(e){}
       return false;};
     scan(document,0);})()""")
    time.sleep(1.2)

    # most ATS keep the form on its own URL behind a plain <a>; follow it rather than click
    if js("document.querySelectorAll('input,textarea').length") < 3:
        href = js(r"""(() => {const a=[...document.querySelectorAll('a')].find(x =>
          /^(apply for this job|apply now|apply|jetzt bewerben|bewerben|bewerbung)$/i
            .test((x.textContent||'').trim()) && x.getAttribute('href'));
          return a ? a.href : '';})()""")
        if href:
            goto(href, timeout=30.0)
            time.sleep(2.5)
            res["note"] = "followed apply link"

    s = form_schema()
    fields = s.get("fields", [])
    res["fields_seen"] = len(fields)
    res["submit_labels"] = (s.get("verdict") or {}).get("submit_labels", [])[:2]

    plan = []
    for f in fields:
        if f.get("needs_interaction") or f.get("options"):
            continue
        val = pick(f.get("label"))
        if not val:
            continue
        step = {"ref": f["ref"], "value": val}
        if any(k in str(f.get("label", "")).lower() for k in ("telefon", "phone", "mobil")):
            step["mode"] = "insert"
        plan.append(step)

    if plan:
        out = fill_form(plan)
        res["attempted"] = out.observed.get("attempted", 0)
        res["filled"] = out.observed.get("succeeded", 0)
        res["failures"] = [{"cls": f.cls.value, "want": f.observed.get("want"),
                            "got": f.observed.get("got")} for f in (out.failures or [])][:3]
    else:
        res["note"] = (res["note"] + "; no mappable fields").strip("; ")

    # NO CV UPLOAD, by instruction: attaching a file POSTs it to the ATS immediately,
    # which happens on selection and not on submit.
    res["cv_uploaded"] = False

    # Readable proof, learned the hard way:
    #  - max_dim DOWNSCALES the capture (it lowers clip.scale), so passing it shrinks text
    #  - JPEG q70 smears small form labels; PNG is lossless and these are flat UI shots
    #  - a 1500px-wide window leaves the form floating in whitespace, so narrow the
    #    viewport and make it tall: the form fills the frame and more of it fits in one shot
    js("window.scrollTo(0, 0)")
    with contextlib.suppress(Exception):     # the override is a nicety, not a requirement
        cdp("Emulation.setDeviceMetricsOverride",
            {"width": 1150, "height": 1500, "deviceScaleFactor": 1, "mobile": False})
        time.sleep(0.8)
    capture_screenshot(SHOT)                 # no max_dim -> 1:1 with CSS pixels
    with contextlib.suppress(Exception):
        cdp("Emulation.clearDeviceMetricsOverride")
    res["shot"] = os.path.basename(SHOT)
    res["url_final"] = js("location.href")[:150]
except HarnessError as e:
    res["error"] = f"{e.cls.value}: {str(e)[:90]}"
except Exception as e:  # noqa: BLE001 — every outcome must still print a RESULT line
    res["error"] = f"{type(e).__name__}: {str(e)[:90]}"
finally:
    # Tab hygiene is not optional here: ten parallel clients leaking tabs across runs
    # exhausted the browser until it dropped the connection mid-session.
    with contextlib.suppress(Exception):
        close_tab()

print("RESULT " + json.dumps(res, ensure_ascii=False, default=str))
