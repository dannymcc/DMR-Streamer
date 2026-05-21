from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "app" / "main.py"


class ServiceWorkerSourceTest(unittest.TestCase):
    def test_navigation_cache_only_updates_root_shell(self):
        source = MAIN.read_text()

        self.assertIn('url.pathname === "/" && response.ok', source)
        self.assertIn('cache.put("/", copy)', source)

    def test_vendor_assets_are_precached_and_used(self):
        main_source = MAIN.read_text()
        base_source = (ROOT / "app" / "templates" / "base.html").read_text()

        self.assertIn('"/static/vendor/htmx.min.js"', main_source)
        self.assertIn('"/static/vendor/tailwindcss.js"', main_source)
        self.assertIn('src="/static/vendor/htmx.min.js?v={{ app_version }}"', base_source)
        self.assertIn('src="/static/vendor/tailwindcss.js?v={{ app_version }}"', base_source)

    def test_playback_banner_uses_audio_delay_by_default(self):
        main_source = MAIN.read_text()
        index_source = (ROOT / "app" / "templates" / "index.html").read_text()

        self.assertIn("delay_ms: int = 1000", main_source)
        self.assertIn("const PLAYBACK_BANNER_DELAY_MS = 1000;", index_source)
        self.assertIn("let activeCallDelayMs = PLAYBACK_BANNER_DELAY_MS;", index_source)
        self.assertNotIn("syncBannerToNow", index_source)

    def test_uk_chat_talkgroups_are_grouped(self):
        main_source = MAIN.read_text()
        active_call_source = (ROOT / "app" / "templates" / "_active_call.html").read_text()

        self.assertIn('2351: "UK Chat 1"', main_source)
        self.assertIn('2352: "UK Chat 2"', main_source)
        self.assertIn('2353: "UK Chat 3"', main_source)
        self.assertNotIn('2354: "UK Chat 4"', main_source)
        self.assertIn("UK_CHAT_TGS = {2351, 2352, 2353}", main_source)
        self.assertIn('"uk_chat_tgs"', main_source)
        self.assertIn("{% for tg, tg_name in uk_chat_tgs %}", active_call_source)

    def test_usa_nationwide_talkgroup_is_monitored(self):
        main_source = MAIN.read_text()
        config_source = (ROOT / "app" / "config.py").read_text()
        compose_source = (ROOT / "docker-compose.yml").read_text()
        rx_source = (ROOT / "dmr-rx" / "rewind.py").read_text()

        self.assertIn('3100: "USA Nationwide"', main_source)
        self.assertIn('"us": {"label": "US", "enabled": {3100}}', main_source)
        self.assertIn("91,2350,2351,2352,2353,235,3100,23520,23526,23531,23562,235175", config_source)
        self.assertIn("91,2350,2351,2352,2353,235,3100,23520,23526,23531,23562,235175", compose_source)
        self.assertIn("91,2350,2351,2352,2353,235,3100,23520,23526,23531,23562,235175", rx_source)

    def test_worldwide_talkgroup_is_monitored_and_muted_by_default(self):
        main_source = MAIN.read_text()
        config_source = (ROOT / "app" / "config.py").read_text()
        compose_source = (ROOT / "docker-compose.yml").read_text()
        env_example_source = (ROOT / ".env.example").read_text()

        self.assertIn('91: "Worldwide"', main_source)
        self.assertIn('default_muted_tgs: str = "91"', config_source)
        self.assertIn("def default_muted_tg_set(self) -> set[int]:", config_source)
        self.assertIn("return settings.default_muted_tg_set()", main_source)
        self.assertIn("DEFAULT_MUTED_TGS: ${DEFAULT_MUTED_TGS:-91}", compose_source)
        self.assertIn("DEFAULT_MUTED_TGS=91", env_example_source)

    def test_response_relevant_nw_talkgroups_are_monitored(self):
        main_source = MAIN.read_text()

        self.assertIn('23531: "RAYNET UK"', main_source)
        self.assertIn('23562: "M62 Corridor"', main_source)
        self.assertIn('"nw": {"label": "NW", "enabled": {23520, 23531, 23562, 235175}}', main_source)

    def test_temporary_talkgroups_are_sidecar_driven(self):
        main_source = MAIN.read_text()
        rx_source = (ROOT / "dmr-rx" / "rewind.py").read_text()
        index_source = (ROOT / "app" / "templates" / "index.html").read_text()
        active_call_source = (ROOT / "app" / "templates" / "_active_call.html").read_text()

        self.assertIn('TG_EXTRA_FILE = "/data/tg_extra.json"', main_source)
        self.assertIn('@app.post("/api/tg-extra"', main_source)
        self.assertIn('@app.delete("/api/tg-extra/{tg}"', main_source)
        self.assertIn('id="temp-tg-form"', index_source)
        self.assertIn('id="temp-tg-toggle"', index_source)
        self.assertIn('hx-post="/api/tg-extra"', index_source)
        self.assertIn('hx-delete="/api/tg-extra/{{ tg }}"', active_call_source)
        self.assertIn('placeholder="Temporary TG"', index_source)
        self.assertIn('class="mb-2 hidden items-center gap-2', index_source)
        self.assertIn("function toggleTempTgPanel()", index_source)
        self.assertIn("document.body.classList.toggle('temp-tg-open', open)", index_source)
        self.assertIn("event.detail.elt.reset()", index_source)
        self.assertIn("setTempTgPanel(false)", index_source)
        self.assertNotIn('hx-post="/api/tg-extra"', active_call_source)
        self.assertNotIn(">Temporary</h3>", active_call_source)
        self.assertIn('def _refresh_extra_tgs(self):', rx_source)
        self.assertIn('"tg_extra.json"', rx_source)
        self.assertIn('self._subscribe_missing_tgs()', rx_source)

    def test_main_talkgroup_cards_are_compact(self):
        active_call_source = (ROOT / "app" / "templates" / "_active_call.html").read_text()

        self.assertIn("{% if compact %}min-h-[52px]{% else %}min-h-[58px]{% endif %}", active_call_source)
        self.assertIn("{% if compact %}px-2 py-1.5{% else %}px-2.5 py-2{% endif %}", active_call_source)
        self.assertIn("{% if compact %}text-[12px]{% else %}text-[13px]{% endif %}", active_call_source)
        self.assertIn('class="grid grid-cols-3 gap-1.5"', active_call_source)

    def test_bottom_live_banner_is_not_rendered(self):
        index_source = (ROOT / "app" / "templates" / "index.html").read_text()
        active_call_source = (ROOT / "app" / "templates" / "_active_call.html").read_text()

        self.assertNotIn("now-transmitting", index_source)
        self.assertNotIn("now-transmitting", active_call_source)
        self.assertNotIn("hx-swap-oob", active_call_source)

    def test_footer_status_stats_row_is_not_rendered(self):
        index_source = (ROOT / "app" / "templates" / "index.html").read_text()
        base_source = (ROOT / "app" / "templates" / "base.html").read_text()

        self.assertNotIn('id="status"', index_source)
        self.assertNotIn('id="bandwidth"', index_source)
        self.assertNotIn('id="delay-label"', index_source)
        self.assertNotIn("updateBandwidth", index_source)
        self.assertNotIn("toggleSyncDebug", index_source)
        self.assertIn("--footer-height: 58px;", base_source)
        self.assertIn("body.temp-tg-open", base_source)
        self.assertIn("--footer-height: 112px;", base_source)
        self.assertIn("padding-bottom: var(--footer-height);", base_source)
        self.assertIn(".app-footer-inner { padding-bottom: 0; }", base_source)

    def test_radio_svg_icon_is_used(self):
        main_source = MAIN.read_text()
        base_source = (ROOT / "app" / "templates" / "base.html").read_text()
        icon_source = (ROOT / "app" / "static" / "icon.svg").read_text()

        self.assertIn('APP_VERSION = "2026.05.21.7"', main_source)
        self.assertIn('href="/manifest.webmanifest?v={{ app_version }}"', base_source)
        self.assertIn('f"/static/icon-192.png?v={APP_VERSION}"', main_source)
        self.assertIn('response.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"', main_source)
        self.assertIn("https://www.svgrepo.com/svg/485868/radio", icon_source)
        self.assertIn("<title>Radio</title>", icon_source)

    def test_screen_wake_lock_tracks_playback(self):
        index_source = (ROOT / "app" / "templates" / "index.html").read_text()

        self.assertIn("navigator.wakeLock.request('screen')", index_source)
        self.assertIn("function requestWakeLock()", index_source)
        self.assertIn("function releaseWakeLock()", index_source)
        self.assertIn("requestWakeLock();", index_source)
        self.assertIn("releaseWakeLock();", index_source)

    def test_selected_pwa_capabilities_are_enabled(self):
        main_source = MAIN.read_text()
        index_source = (ROOT / "app" / "templates" / "index.html").read_text()
        active_call_source = (ROOT / "app" / "templates" / "_active_call.html").read_text()

        self.assertIn('"orientation": "portrait-primary"', main_source)
        self.assertIn('"launch_handler": {"client_mode": "focus-existing"}', main_source)
        self.assertIn('"shortcuts": [', main_source)
        self.assertIn('"url": f"/?action=temp-tg&v={APP_VERSION}"', main_source)
        self.assertIn("function handleLaunchAction()", index_source)
        self.assertIn("function updateMediaSession()", index_source)
        self.assertIn("navigator.mediaSession.metadata = new MediaMetadata", index_source)
        self.assertIn("DEFAULT_MEDIA_ARTIST", index_source)
        self.assertIn('data-media-active="{{', active_call_source)

    def test_temp_tg_footer_chips_are_refreshed_separately(self):
        main_source = MAIN.read_text()
        index_source = (ROOT / "app" / "templates" / "index.html").read_text()
        chip_source = (ROOT / "app" / "templates" / "_temp_tg_chips.html").read_text()

        self.assertIn('@app.get("/api/temp-tg-chips"', main_source)
        self.assertIn('id="temp-tg-chips"', index_source)
        self.assertIn("function refreshTempTgChips()", index_source)
        self.assertIn("document.body.classList.toggle('temp-tg-has-chips'", index_source)
        self.assertIn('hx-delete="/api/tg-extra/{{ tg }}"', chip_source)

    def test_mute_until_quiet_is_compact_icon(self):
        main_source = MAIN.read_text()
        active_call_source = (ROOT / "app" / "templates" / "_active_call.html").read_text()

        self.assertIn('@app.delete("/api/tg-temp-mutes/{tg}"', main_source)
        self.assertIn('hx-delete="/api/tg-temp-mutes/{{ tg }}"', active_call_source)
        self.assertIn('aria-label="Mute until quiet"', active_call_source)
        self.assertIn('class="h-9 w-9 shrink-0 rounded-lg text-white flex items-center justify-center', active_call_source)
        self.assertIn('{% if active_muted %}bg-neutral-800 border border-neutral-700', active_call_source)
        self.assertIn('{% else %}bg-glove-accent/40 border border-glove-accent{% endif %}', active_call_source)
        self.assertIn('stroke="currentColor"', active_call_source)
        self.assertNotIn("🔇", active_call_source)
        self.assertNotIn(">Mute until quiet</button>", active_call_source)

    def test_diagnostics_page_exposes_runtime_status(self):
        main_source = MAIN.read_text()
        diag_source = (ROOT / "app" / "templates" / "diagnostics.html").read_text()

        self.assertIn('@app.get("/diagnostics"', main_source)
        self.assertIn('"diagnostics.html"', main_source)
        self.assertIn("service_worker_version", main_source)
        self.assertIn("PWA cache", diag_source)
        self.assertIn("Sidecars", diag_source)

    def test_splash_screen_is_centered_logo_only(self):
        base_source = (ROOT / "app" / "templates" / "base.html").read_text()
        splash_source = (ROOT / "app" / "static" / "splash.svg").read_text()

        self.assertIn('href="/static/splash.svg?v={{ app_version }}"', base_source)
        self.assertIn('viewBox="0 0 1290 2796"', splash_source)
        self.assertIn('transform="translate(389 1142)"', splash_source)
        self.assertNotIn("<text", splash_source)

    def test_receiver_ignores_kerchunks(self):
        rx_source = (ROOT / "dmr-rx" / "rewind.py").read_text()
        compose_source = (ROOT / "docker-compose.yml").read_text()

        self.assertIn("kerchunk_min_seconds: float = 1.0", rx_source)
        self.assertIn("KERCHUNK_MIN_SECONDS", compose_source)
        self.assertIn("ignored kerchunk", rx_source)
        self.assertIn("self._call_has_enough_audio()", rx_source)


if __name__ == "__main__":
    unittest.main()
