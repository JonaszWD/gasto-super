// Camera barcode scanning with @zxing/browser (iOS Safari has no native BarcodeDetector).
import { t } from "./i18n.js";

export class Scanner {
  /** @param {HTMLVideoElement} video */
  constructor(video, onCode) {
    this.video = video;
    this.onCode = onCode;
    this.controls = null;
    this.reader = null;
    this.starting = null;
  }

  get running() {
    return this.controls !== null;
  }

  start() {
    // Coalesce concurrent calls so we never open two camera streams.
    this.starting ??= this._start().finally(() => (this.starting = null));
    return this.starting;
  }

  async _start() {
    if (this.running) return;
    this.cancelled = false;
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      throw new Error(window.isSecureContext ? t("scan.no_camera") : t("scan.need_https"));
    }
    if (!window.ZXingBrowser) throw new Error(t("scan.lib_missing"));
    // iOS only plays inline video with these set (also present in the markup).
    this.video.setAttribute("playsinline", "");
    this.video.setAttribute("autoplay", "");
    this.video.muted = true;

    // 1D-only reader: EAN-13/8 and UPC, faster than the multi-format one.
    this.reader ??= new window.ZXingBrowser.BrowserMultiFormatOneDReader(undefined, {
      delayBetweenScanAttempts: 80,
      delayBetweenScanSuccess: 800,
    });
    const constraints = {
      audio: false,
      video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } },
    };
    try {
      const controls = await this.reader.decodeFromConstraints(constraints, this.video, (result) => {
        if (!result || !this.controls) return;
        const code = result.getText();
        this.stop();
        this.onCode(code);
      });
      if (this.cancelled) {
        controls.stop(); // stop() was called while the camera was starting
        return;
      }
      this.controls = controls;
    } catch (err) {
      this.controls = null;
      console.error(err);
      throw new Error(t("scan.no_camera"));
    }
  }

  stop() {
    this.cancelled = true;
    if (this.controls) {
      this.controls.stop();
      this.controls = null;
    }
  }
}

/** Visual flash plus vibration where supported (Android) or the iOS 18 switch-haptic trick. */
export function scanFeedback() {
  const flash = document.getElementById("flash");
  flash.classList.remove("on");
  void flash.offsetWidth; // restart animation
  flash.classList.add("on");
  if (navigator.vibrate) {
    navigator.vibrate(60);
  } else {
    document.getElementById("haptic")?.click();
  }
}
