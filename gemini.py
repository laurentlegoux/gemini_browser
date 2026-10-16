import lvgl as lv  
import sys  
import asyncio  
import mpos.ui  
from mpos import (  
    Activity, DisplayMetrics, MposKeyboard, TaskManager, AppearanceManager,  
    SharedPreferences, SettingsActivity, Intent, AppManager,  
)  
import gemini_client  
  
_MAX_HISTORY = 20  
_DEFAULT_SEARCH_PREFIX = "gemini://tlgs.one/search?"  
_DEFAULT_SCHEME = "gemini://"  
  
_UNRESERVED = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"  
  
  
def _load_about_text(app_fullname):  
    """Read about.gmi from this app's installed directory.  
  
    Standalone function (not a method) because it's needed by both  
    Gemini and AboutActivity, and doesn't need any instance state besides  
    the app's fullname, which each Activity already has as self.appFullName.  
    """  
    app = AppManager.get(app_fullname) if app_fullname else None  
    about_path = (app.installed_path + "/about.gmi") if app and app.installed_path else "about.gmi"  
    try:  
        with open(about_path, "r") as f:  
            return f.read()  
    except OSError:  
        return "# About\n\n(about.gmi not found)"  
  
  
def _percent_encode(text):  
    """RFC3986 percent-encoding, including spaces, for a Gemini input query component."""  
    out = []  
    for ch in text:  
        b = ch.encode("utf-8")  
        if len(b) == 1 and chr(b[0]) in _UNRESERVED:  
            out.append(chr(b[0]))  
        else:  
            for byte in b:  
                out.append("%%%02X" % byte)  
    return "".join(out)  
  
  
def _is_local_path(text):  
    """A path to a file on the local filesystem, as handed to us by the  
    file explorer's "view" intent (e.g. "./data/text/test.gmi",  
    "/sd/notes/test.gmi"). These must NEVER be run through  
    _looks_like_uri()/_normalize_url() -- they have no network host, so  
    "gemini://" must never be prepended, and gemini_client.fetch() must  
    never be called on them (it tries to resolve a DNS host and fails  
    with "addrinfo error -2" since there's no host to resolve)."""  
    return text.startswith("/") or text.startswith("./") or text.startswith("../")  
  
  
def _looks_like_uri(text):  
    """Heuristic used to decide between "fetch as URI" and "run as search".  
  
    - Anything with an explicit "scheme://" is always a URI.  
    - Otherwise, treat it as a URI too if it looks like a bare host  
      ("server.tld", "server.tld/path", "server.tld:1965/path"): no  
      whitespace, and the host part contains a dot. _normalize_url() is  
      responsible for prepending "gemini://" to these before fetching.  
    - Anything else (no dot in the host part, or contains whitespace) is  
      treated as a search query instead.  
  
    NOTE: callers must check _is_local_path() BEFORE this function --  
    a local path like "./data/text/test.gmi" would otherwise match the  
    bare-host heuristic (host == ".", which contains a dot).  
    """  
    if "://" in text:  
        return True  
    if not text or " " in text or "\t" in text:  
        return False  
    host = text.split("/", 1)[0].split(":", 1)[0]  
    return "." in host  
  
  
def _normalize_url(text):  
    """Add the "gemini://" scheme when the user typed a bare host/path  
    with no explicit scheme (e.g. "geminiprotocol.net" -> "gemini://geminiprotocol.net").  
    Never call this on a local path -- check _is_local_path() first."""  
    if "://" not in text:  
        return _DEFAULT_SCHEME + text  
    return text  
  
  
def _resolve_local_link(base_path, link):  
    """Resolve a link target found inside a locally-opened .gmi file.  
  
    - A link with its own "scheme://" (e.g. "gemini://...") is left as-is;  
      _is_local_path() will be False for it, so _fetch() routes it over  
      the network as usual.  
    - An absolute local path ("/...") is left as-is.  
    - Anything else is treated as relative to base_path's own directory  
      on disk (simple join, no "../" collapsing -- gemtext files rarely  
      need that and open() will just fail loudly if it's wrong).  
    """  
    if "://" in link or link.startswith("/"):  
        return link  
    base_dir = base_path.rsplit("/", 1)[0] if "/" in base_path else "."  
    return base_dir + "/" + link  
  
  
class FavoritesActivity(Activity):  
    """Minimal list of starred Gemini URLs, opened from the 'Favorites' row in  
    SettingsActivity.  
  
    IMPORTANT: SettingsActivity.openSettingActivity() only forwards two  
    extras to an "activity"-type setting row: "setting" and "prefs". It does  
    NOT forward arbitrary extras like "favorites"/"on_select"/"on_delete".  
    So this activity must be fully self-sufficient using only "prefs" (the  
    same SharedPreferences instance Gemini uses) instead of expecting data  
    or callbacks to be handed to it directly.  
    """  
  
    def onCreate(self):  
        extras = self.getIntent().extras or {}  
        self.prefs = extras.get("prefs")  
        if self.prefs is None:  
            # Fallback in case this is ever launched without the "prefs" extra.  
            self.prefs = SharedPreferences(self.appFullName)  
        self._favorites = self.prefs.get_list("favorites", [])  
  
        screen = lv.obj()  
        screen.set_style_pad_all(0, lv.PART.MAIN)  
        screen.set_flex_flow(lv.FLEX_FLOW.COLUMN)  
        screen.remove_flag(lv.obj.FLAG.SCROLLABLE)  
  
        header = lv.obj(screen)  
        header.set_width(lv.pct(100))  
        header.set_height(lv.SIZE_CONTENT)  
        header.set_style_pad_all(DisplayMetrics.pct_of_width(2), lv.PART.MAIN)  
        header.set_flex_flow(lv.FLEX_FLOW.ROW)  
        header.set_style_flex_main_place(lv.FLEX_ALIGN.SPACE_BETWEEN, lv.PART.MAIN)  
        header.set_style_border_width(0, lv.PART.MAIN)  
  
        back_btn = lv.button(header)  
        back_btn.set_size(DisplayMetrics.pct_of_width(12), DisplayMetrics.pct_of_width(12))  
        back_lbl = lv.label(back_btn)  
        back_lbl.set_text(lv.SYMBOL.LEFT)  
        back_lbl.center()  
        back_btn.add_event_cb(lambda e: self.finish(), lv.EVENT.CLICKED, None)  
  
        title = lv.label(header)  
        title.set_text("Favorites")  
        title.set_style_text_font(lv.font_montserrat_18, lv.PART.MAIN)  
  
        self._list = lv.list(screen)  
        self._list.set_width(lv.pct(100))  
        self._list.set_flex_grow(1)  
  
        self.setContentView(screen)  
        self._refresh()  
  
    def _refresh(self):  
        self._list.clean()  
        if not self._favorites:  
            btn = self._list.add_button(None, "No favorites saved yet.")  
            btn.add_state(lv.STATE.DISABLED)  
            return  
        for fav in list(self._favorites):  
            url = fav.get("url")  
            text = fav.get("title") or url  
            btn = self._list.add_button(lv.SYMBOL.CLOSE, text)  
            btn.add_event_cb(lambda e, u=url: self._select(u), lv.EVENT.CLICKED, None)  
            # The leading icon slot doubles as a delete affordance: clicking  
            # it specifically removes the favorite instead of navigating.  
            icon = btn.get_child(0)  
            if icon is not None:  
                icon.add_flag(lv.obj.FLAG.CLICKABLE)  
                icon.add_event_cb(lambda e, u=url: self._delete(u), lv.EVENT.CLICKED, None)  
  
    def _save_favorites(self):  
        self.prefs.edit().put_list("favorites", self._favorites).commit()  
  
    def _select(self, url):  
        # Navigate back to Gemini with this URL. This launches a fresh  
        # Gemini Activity the same way the app already handles  
        # intent.data in onCreate() when opened from outside.  
        intent = Intent(activity_class=Gemini, data=url)  
        self.startActivity(intent)  
        self.finish()  
  
    def _delete(self, url):  
        self._favorites = [f for f in self._favorites if f.get("url") != url]  
        self._save_favorites()  
        self._refresh()  
  
  
class AboutActivity(Activity):  
    """Renders about.gmi (stored alongside gemini.py in this app's own  
    folder) as plain gemtext, reusing gemini_client.format_gemtext().  
  
    Opened from the 'About' row in SettingsActivity via the same  
    ui: 'activity' pattern as FavoritesActivity. Like FavoritesActivity,  
    SettingsActivity only forwards 'setting' and 'prefs' as extras, so  
    this activity doesn't depend on anything but self.appFullName.  
    """  
  
    def onCreate(self):  
        screen = lv.obj()  
        screen.set_style_pad_all(0, lv.PART.MAIN)  
        screen.set_flex_flow(lv.FLEX_FLOW.COLUMN)  
        screen.remove_flag(lv.obj.FLAG.SCROLLABLE)  
  
        header = lv.obj(screen)  
        header.set_width(lv.pct(100))  
        header.set_height(lv.SIZE_CONTENT)  
        header.set_style_pad_all(DisplayMetrics.pct_of_width(2), lv.PART.MAIN)  
        header.set_flex_flow(lv.FLEX_FLOW.ROW)  
        header.set_style_flex_main_place(lv.FLEX_ALIGN.SPACE_BETWEEN, lv.PART.MAIN)  
        header.set_style_border_width(0, lv.PART.MAIN)  
  
        back_btn = lv.button(header)  
        back_btn.set_size(DisplayMetrics.pct_of_width(12), DisplayMetrics.pct_of_width(12))  
        back_lbl = lv.label(back_btn)  
        back_lbl.set_text(lv.SYMBOL.LEFT)  
        back_lbl.center()  
        back_btn.add_event_cb(lambda e: self.finish(), lv.EVENT.CLICKED, None)  
  
        title = lv.label(header)  
        title.set_text("About")  
        title.set_style_text_font(lv.font_montserrat_18, lv.PART.MAIN)  
  
        content = lv.obj(screen)  
        content.set_width(lv.pct(100))  
        content.set_flex_grow(1)  
        content.set_flex_flow(lv.FLEX_FLOW.COLUMN)  
        content.set_style_pad_all(8, lv.PART.MAIN)  
        content.set_style_pad_row(6, lv.PART.MAIN)  
  
        text_color = lv.color_hex(0x000000) if AppearanceManager.is_light_mode() else lv.color_hex(0xFFFFFF)  
        primary = lv.theme_get_color_primary(None)  
        font_for = {  
            "h1": lv.font_montserrat_18,  
            "h2": lv.font_montserrat_16,  
            "h3": lv.font_montserrat_14,  
        }  
  
        about_text = _load_about_text(self.appFullName)  
        for kind, value in gemini_client.format_gemtext(about_text):  
            if kind == "blank":  
                continue  
            if kind == "link":  
                url, label_text = value  
                text = "%s (%s)" % (label_text, url)  
            else:  
                text = value  
  
            label = lv.label(content)  
            label.set_width(lv.pct(100))  
            label.set_long_mode(lv.label.LONG_MODE.WRAP)  
            label.set_text(text)  
            label.set_style_pad_top(0, lv.PART.MAIN)  
            label.set_style_pad_bottom(0, lv.PART.MAIN)  
            if kind in ("h1", "h2", "h3"):  
                label.set_style_text_font(font_for[kind], lv.PART.MAIN)  
                label.set_style_text_color(primary, lv.PART.MAIN)  
            else:  
                label.set_style_text_font(lv.font_montserrat_14, lv.PART.MAIN)  
                label.set_style_text_color(text_color, lv.PART.MAIN)  
  
        self.setContentView(screen)  
  
  
class Gemini(Activity):  
  
    def onCreate(self):  
        self._current_url = None  
        screen = lv.obj()  
        screen.remove_flag(lv.obj.FLAG.SCROLLABLE)  
        screen.set_flex_flow(lv.FLEX_FLOW.COLUMN)  
        screen.set_style_pad_gap(4, 0)  
  
        self._history = []  # stack of previously visited URLs (max 20)  
        self._navigating_back = False  
        self._input_ta = None  # active 1x INPUT textarea, if any  
        self._input_target_url = None  
        self._input_overlay = None  # floating lv.obj on layer_top, if an INPUT prompt is active  
  
        # Preferences: search engine prefix + favorites list.  
        self.prefs = SharedPreferences(self.appFullName)  
        self._search_prefix = self.prefs.get_string("search_prefix", _DEFAULT_SEARCH_PREFIX)  
        self._favorites = self.prefs.get_list("favorites", [])  # [{"url":..., "title":...}, ...]  
  
        self._top_bar = lv.obj(screen)  
        self._top_bar.remove_flag(lv.obj.FLAG.SCROLLABLE)  
        self._top_bar.set_size(lv.pct(100), lv.SIZE_CONTENT)  
        self._top_bar.set_style_pad_all(4, lv.PART.MAIN)  
        self._top_bar.set_style_pad_column(2, lv.PART.MAIN)  
        self._top_bar.set_flex_flow(lv.FLEX_FLOW.ROW)  
        self._top_bar.set_flex_align(  
            lv.FLEX_ALIGN.SPACE_BETWEEN, lv.FLEX_ALIGN.CENTER, lv.FLEX_ALIGN.CENTER  
        )  
  
        def _icon_btn(parent, symbol):  
            btn = lv.button(parent)  
            btn.set_size(  
                DisplayMetrics.pct_of_width(11), DisplayMetrics.pct_of_height(13)  
            )  
            label = lv.label(btn)  
            label.set_text(symbol)  
            label.center()  
            return btn, label  
  
        # ---- Back button (leftmost widget in the toolbar) ----  
        self.back_btn, _ = _icon_btn(self._top_bar, lv.SYMBOL.LEFT)  
        self.back_btn.add_event_cb(self._on_back, lv.EVENT.CLICKED, None)  
        self.back_btn.add_state(lv.STATE.DISABLED)  # nothing to go back to yet  
  
        self.url_bar = lv.textarea(self._top_bar)  
        self.url_bar.set_flex_grow(1)  
        self.url_bar.set_height(40)  
        self.url_bar.set_one_line(True)  
        self.url_bar.set_text("gemini://geminiprotocol.net")  
        self.url_bar.add_state(lv.STATE.FOCUSED)  
        self.url_bar.add_event_cb(  
            lambda e: self._show_keyboard_for(self.url_bar), lv.EVENT.CLICKED, None  
        )  
  
        try:  
            intent = self.getIntent()  
            if intent and intent.data:  
                self.url_bar.set_text(intent.data)  
        except AttributeError:  
            pass  
  
        # Normalize whatever ended up in the url bar (default text or  
        # intent.data) so a bare "server.tld" passed in from outside also  
        # gets "gemini://" prepended before the very first fetch.  
        # Local filesystem paths (e.g. opened from the file explorer) must  
        # be left completely untouched -- see _is_local_path()'s docstring  
        # for why they must never go through _looks_like_uri()/_normalize_url().  
        initial_text = self.url_bar.get_text().strip()  
        if initial_text and not _is_local_path(initial_text) and _looks_like_uri(initial_text):  
            initial_text = _normalize_url(initial_text)  
            self.url_bar.set_text(initial_text)  
  
        # "Go" -> relabeled with a right-arrow symbol. Now does double duty:  
        # fetch a URI directly, or run a search if the text isn't a URI.  
        go_btn, _ = _icon_btn(self._top_bar, lv.SYMBOL.RIGHT)  
        go_btn.add_event_cb(self._on_go, lv.EVENT.CLICKED, None)  
  
        # Star -> toggles the current URL in favorites. Hidden entirely once  
        # the current page IS a favorite (nothing left to "add").  
        self.star_btn, self._star_label = _icon_btn(self._top_bar, mpos.ui.STAR_SYMBOL)  
        self.star_btn.add_event_cb(self._on_star, lv.EVENT.CLICKED, None)  
  
        # Preferences (rightmost) -> standard SettingsActivity, same pattern  
        # as ChatListActivity._settings(): a declarative list of rows, one  
        # of which opens FavoritesActivity, another opens AboutActivity.  
        prefs_btn, _ = _icon_btn(self._top_bar, lv.SYMBOL.SETTINGS)  
        prefs_btn.add_event_cb(self._on_show_preferences, lv.EVENT.CLICKED, None)  
  
        # ---- Content: one scrollable column of lv.label widgets, one per gemtext line ----  
        self._content_container = lv.obj(screen)  
        self._content_container.set_width(lv.pct(100))  
        self._content_container.set_flex_grow(1)  
        self._content_container.set_flex_flow(lv.FLEX_FLOW.COLUMN)  
        self._content_container.set_style_pad_all(4, lv.PART.MAIN)  
        self._content_container.set_style_pad_row(6, lv.PART.MAIN)  
  
        self._styles = self._build_styles()  
  
        self.keyboard = MposKeyboard(screen)  
        self.keyboard.set_textarea(self.url_bar)  
        #self.keyboard.set_style_min_height(0, lv.PART.MAIN)  
        #self.keyboard.set_size(lv.pct(100), DisplayMetrics.pct_of_height(40))  
        self.keyboard.add_flag(lv.obj.FLAG.HIDDEN)  
        self.keyboard.add_event_cb(self._on_keyboard_ready, lv.EVENT.READY, None) 
  
        self.setContentView(screen)  
  
        self._update_star_button()  
        TaskManager.create_task(self._fetch(self.url_bar.get_text()))  
  
    def _show_keyboard_for(self, textarea):  
        self.keyboard.set_textarea(textarea)  
        self.keyboard.show_keyboard()  
        if textarea is self._input_ta and self._input_overlay is not None:  
            lv.timer_create(lambda t: self._position_input_overlay(), 60, None).set_repeat_count(1)  
  
    def _on_keyboard_ready(self, event):  
        # Fired when the keyboard's "validate"/Enter key is pressed,  
        # regardless of which textarea is currently attached to it.  
        self.keyboard.add_flag(lv.obj.FLAG.HIDDEN)  
        active_ta = self.keyboard.get_textarea()  
        if self._input_ta is not None and active_ta == self._input_ta:  
            # Validating the 1x INPUT prompt == pressing "Submit".  
            self._on_input_submit(event, self._input_target_url)  
        elif active_ta == self.url_bar:  
            # Validating the URL bar == pressing "Go".  
            self._on_go(event)  
  
    def _build_styles(self):  
        styles = {}  
        font_map = {  
            14: lv.font_montserrat_14,  
            16: lv.font_montserrat_16,  
            18: lv.font_montserrat_18,  
        }  
        primary = lv.theme_get_color_primary(None)  
        normal_text = lv.color_hex(0x000000) if AppearanceManager.is_light_mode() else lv.color_hex(0xFFFFFF)  
        muted_text = lv.palette_main(lv.PALETTE.GREY)  
  
        for kind, (size, color) in {  
            "h1": (16, primary),  
            "h2": (16, primary),  
            "h3": (14, primary),  
            "link": (14, primary),  
            "list": (14, normal_text),  
            "quote": (14, muted_text),  
            "pre": (14, normal_text),  
            "text": (16, normal_text),  
            "blank": (14, normal_text),  
        }.items():  
            style = lv.style_t()  
            style.init()  
            style.set_text_color(color)  
            style.set_text_font(font_map.get(size, lv.font_montserrat_14))  
            style.set_pad_top(0)  
            style.set_pad_bottom(0)  
            if kind == "blank":  
                style.set_height(6)  
            styles[kind] = style  
        return styles  
  
    def _clear_content(self):  
        self._dismiss_input_overlay()  
        while self._content_container.get_child_count() > 0:  
            self._content_container.get_child(0).delete()  
  
    def _on_go(self, event):  
        text = self.url_bar.get_text().strip()  
        if not text:  
            return  
        if _is_local_path(text):  
            # Local filesystem path: fetch as-is, no normalization, no  
            # network. (See _is_local_path()'s docstring.)  
            TaskManager.create_task(self._fetch(text))  
        elif _looks_like_uri(text):  
            url = _normalize_url(text)  
            self.url_bar.set_text(url)  
            TaskManager.create_task(self._fetch(url))  
        else:  
            self._do_search(text)  
  
    def _do_search(self, query_text):  
        query = _percent_encode(query_text)  
        search_url = self._search_prefix + query  
        self.url_bar.set_text(search_url)  
        TaskManager.create_task(self._fetch(search_url))  
  
    def _on_back(self, event):  
        if not self._history:  
            return  
        url = self._history.pop()  
        self._navigating_back = True  
        if not self._history:  
            self.back_btn.add_state(lv.STATE.DISABLED)  
        self.url_bar.set_text(url)  
        TaskManager.create_task(self._fetch(url))  
  
    def _push_history(self, url):  
        self._history.append(url)  
        if len(self._history) > _MAX_HISTORY:  
            self._history.pop(0)  
        self.back_btn.remove_state(lv.STATE.DISABLED)  
  
    # ---- Favorites ----  
  
    def _find_favorite_index(self, url):  
        for i, fav in enumerate(self._favorites):  
            if fav.get("url") == url:  
                return i  
        return -1  
  
    def _save_favorites(self):  
        self.prefs.edit().put_list("favorites", self._favorites).commit()  
  
    def _update_star_button(self):  
        is_fav = self._current_url is not None and self._find_favorite_index(self._current_url) >= 0  
        # Only show the star while the current page is NOT already a  
        # favorite -- once it is, there's nothing left to "add".  
        if is_fav:  
            self.star_btn.add_flag(lv.obj.FLAG.HIDDEN)  
        else:  
            self.star_btn.remove_flag(lv.obj.FLAG.HIDDEN)  
  
    def _on_star(self, event):  
        if not self._current_url:  
            return  
        idx = self._find_favorite_index(self._current_url)  
        if idx < 0:  
            title = self.url_bar.get_text() or self._current_url  
            self._favorites.append({"url": self._current_url, "title": title})  
            if len(self._favorites) > 200:  # sane upper bound, independent of history cap  
                self._favorites.pop(0)  
            self._save_favorites()  
        self._update_star_button()  
  
    # ---- Preferences: standard SettingsActivity (same pattern as the nostr app) ----  
  
    def _on_show_preferences(self, event):  
        settings = [  
            {  
                "title": "Search engine",  
                "key": "search_prefix",  
                "placeholder": _DEFAULT_SEARCH_PREFIX,  
                "default_value": _DEFAULT_SEARCH_PREFIX,  
            },  
            {  
                "title": "Favorites",  
                "key": "favorites_activity",  
                "ui": "activity",  
                "activity_class": FavoritesActivity,  
                "dont_persist": True,  
                "placeholder": "View my favorites (%d)" % len(self._favorites),  
            },  
            {  
                "title": "About",  
                "key": "about_activity",  
                "ui": "activity",  
                "activity_class": AboutActivity,  
                "dont_persist": True,  
                "placeholder": "View about.gmi",  
            },  
        ]  
        intent = Intent(activity_class=SettingsActivity)  
        intent.putExtra("prefs", self.prefs)  
        intent.putExtra("settings", settings)  
        self.startActivity(intent)  
  
    def onResume(self, screen=None):  
        # SettingsActivity reads/writes "search_prefix" and FavoritesActivity  
        # reads/writes "favorites" directly on self.prefs; re-sync both  
        # cached copies whenever we come back from either screen so the  
        # toolbar/star button and the "Favorites (%d)" count stay accurate.  
        self._search_prefix = self.prefs.get_string("search_prefix", _DEFAULT_SEARCH_PREFIX)  
        self._favorites = self.prefs.get_list("favorites", [])  
        self._update_star_button()  
  
    def _make_label(self, kind):  
        label = lv.label(self._content_container)  
        label.set_width(lv.pct(100))  
        label.set_long_mode(lv.label.LONG_MODE.WRAP)  
        label.set_style_pad_top(0, lv.PART.MAIN)  
        label.set_style_pad_bottom(0, lv.PART.MAIN)  
        label.add_style(self._styles.get(kind, self._styles["text"]), lv.PART.MAIN)  
        return label  
  
    def _set_status(self, text):  
        self._clear_content()  
        label = self._make_label("text")  
        label.set_text(text)  
  
    def _render_gemtext(self, parsed_lines):  
        self._clear_content()  
        current = self.url_bar.get_text()  
        current_is_local = _is_local_path(current)  
        for kind, value in parsed_lines:  
            text = self._line_text(kind, value)  
            label = self._make_label(kind)  
            label.set_text(text)  
  
            if kind == "link":  
                url, _label_text = value  
                if current_is_local:  
                    # Relative links inside a locally-opened .gmi file are  
                    # resolved against its own directory on disk, not as  
                    # Gemini URLs -- see _resolve_local_link()'s docstring.  
                    target_url = _resolve_local_link(current, url)  
                else:  
                    target_url = (  
                        gemini_client.resolve_url(current, url)  
                        if hasattr(gemini_client, "resolve_url") else url  
                    )  
                label.set_style_text_decor(lv.TEXT_DECOR.UNDERLINE, lv.PART.MAIN | lv.STATE.DEFAULT)  
                label.add_flag(lv.obj.FLAG.CLICKABLE)  
                label.add_event_cb(  
                    lambda e, u=target_url: self._on_link_clicked(e, u),  
                    lv.EVENT.CLICKED, None,  
                )  
  
    def _on_link_clicked(self, event, url):  
        self.url_bar.set_text(url)  
        TaskManager.create_task(self._fetch(url))  
  
    # ---- Gemini 1x INPUT handling (section 3.2.1 of the spec) ----  
  
    def _render_input_prompt(self, prompt_text, target_url, sensitive=False):  
        self._clear_content()  # also calls _dismiss_input_overlay()  
  
        label = self._make_label("text")  
        label.set_text(prompt_text or "Enter a value:")  
  
        overlay = lv.obj(lv.layer_top())  
        overlay.set_width(lv.pct(92))  
        overlay.set_height(lv.SIZE_CONTENT)  
        overlay.set_style_pad_all(8, lv.PART.MAIN)  
        overlay.set_style_pad_row(6, lv.PART.MAIN)  
        overlay.set_flex_flow(lv.FLEX_FLOW.COLUMN)  
        overlay.set_style_radius(8, lv.PART.MAIN)  
        overlay.set_style_border_width(1, lv.PART.MAIN)  
        overlay.set_style_shadow_width(12, lv.PART.MAIN)  
        overlay.align(lv.ALIGN.TOP_MID, 0, 0)  # placeholder, repositioned below  
        self._input_overlay = overlay  
  
        overlay_label = lv.label(overlay)  
        overlay_label.set_width(lv.pct(100))  
        overlay_label.set_long_mode(lv.label.LONG_MODE.WRAP)  
        overlay_label.set_text(prompt_text or "Enter a value:")  
  
        input_row = lv.obj(overlay)  
        input_row.remove_flag(lv.obj.FLAG.SCROLLABLE)  
        input_row.set_width(lv.pct(100))  
        input_row.set_height(lv.SIZE_CONTENT)  
        input_row.set_style_pad_all(0, lv.PART.MAIN)  
        input_row.set_style_pad_column(6, lv.PART.MAIN)  
        input_row.set_style_border_width(0, lv.PART.MAIN)  
        input_row.set_flex_flow(lv.FLEX_FLOW.ROW)  
        input_row.set_flex_align(  
            lv.FLEX_ALIGN.START, lv.FLEX_ALIGN.CENTER, lv.FLEX_ALIGN.CENTER  
        )  
  
        self._input_ta = lv.textarea(input_row)  
        self._input_ta.set_flex_grow(1)  
        self._input_ta.set_height(40)  
        self._input_ta.set_one_line(True)  
        if sensitive:  
            self._input_ta.set_password_mode(True)  
        self._input_ta.add_event_cb(  
            lambda e: self._show_keyboard_for(self._input_ta), lv.EVENT.CLICKED, None  
        )  
  
        submit_btn = lv.button(input_row)  
        submit_btn.set_size(40, 40)  
        submit_btn.add_event_cb(  
            lambda e, u=target_url: self._on_input_submit(e, u),  
            lv.EVENT.CLICKED, None,  
        )  
        submit_label = lv.label(submit_btn)  
        submit_label.set_text(lv.SYMBOL.UPLOAD)  
        submit_label.center()  
  
        self._input_target_url = target_url  
  
        self._show_keyboard_for(self._input_ta)  
        overlay.update_layout()  
        self._position_input_overlay()  
  
    def _position_input_overlay(self):  
        if self._input_overlay is None:  
            return  
        margin = 6  
        self._input_overlay.align_to(self.keyboard._keyboard, lv.ALIGN.OUT_TOP_MID, 0, -margin)  
  
    def _dismiss_input_overlay(self):  
        self._input_ta = None  
        self._input_target_url = None  
        if self._input_overlay is not None:  
            self._input_overlay.delete()  
            self._input_overlay = None  
  
    def _on_input_submit(self, event, target_url):  
        if self._input_ta is None:  
            return  
        query = _percent_encode(self._input_ta.get_text())  
        base = target_url.split("?", 1)[0]  
        new_url = base + "?" + query  
  
        self.keyboard.add_flag(lv.obj.FLAG.HIDDEN)  
        self.keyboard.set_textarea(self.url_bar)  
        self._dismiss_input_overlay()  
  
        self._clear_content()  
        self._set_status("Loading %s ..." % new_url)  
  
        self.url_bar.set_text(new_url)  
        TaskManager.create_task(self._fetch(new_url))  
  
    @staticmethod  
    def _parse_status(status_line):  
        parts = status_line.split(None, 1)  
        code = parts[0] if parts else ""  
        meta = parts[1] if len(parts) > 1 else ""  
        return code, meta  
  
    @staticmethod  
    def _read_local_file(path):  
        """Read a .gmi (or any text) file directly from the local filesystem.  
        Used for paths like "./data/text/test.gmi" handed to us by the file  
        explorer's "view" intent -- never passed through gemini_client.fetch()."""  
        with open(path, "r") as f:  
            return f.read()  
  
    @staticmethod  
    def _line_text(kind, value):  
        if kind == "link":  
            url, label = value  
            return "[%s] (%s)" % (label, url)  
        if kind == "h1":  
            return "# %s" % value  
        if kind == "h2":  
            return "## %s" % value  
        if kind == "h3":  
            return "### %s" % value  
        if kind == "list":  
            return "  * %s" % value  
        if kind == "quote":  
            return "  | %s" % value  
        if kind == "blank":  
            return ""  
        return value  
  
    async def _fetch(self, url):  
        previous_url = self._current_url  
        going_back = self._navigating_back  
        self._navigating_back = False  
  
        self._set_status("Loading %s ..." % url)  
        try:  
            if _is_local_path(url):  
                # Local filesystem path: read directly, no network call,  
                # no status line to parse (always "success").  
                body = self._read_local_file(url)  
                final_url = url  
                self.url_bar.set_text(final_url)  
                parsed = gemini_client.format_gemtext(body)  
                self._render_gemtext(parsed)  
            else:  
                status_line, body, final_url = await gemini_client.fetch(url)  
                self.url_bar.set_text(final_url)  
  
                code, meta = self._parse_status(status_line)  
                if code in ("10", "11"):  
                    self._render_input_prompt(meta, final_url, sensitive=(code == "11"))  
                else:  
                    error = gemini_client.status_text(status_line)  
                    if error:  
                        self._set_status(error)  
                    else:  
                        parsed = gemini_client.format_gemtext(body)  
                        self._render_gemtext(parsed)  
  
            if not going_back and previous_url and previous_url != final_url:  
                self._push_history(previous_url)  
            self._current_url = final_url  
            self._update_star_button()  
        except gemini_client.CrossProtocolRedirect as e:  
            self._set_status("External redirect refused: %s" % e.url)  
        except gemini_client.TooManyRedirects:  
            self._set_status("Too many redirects")  
        except (OSError, asyncio.TimeoutError, ValueError) as e:  
            self._set_status("Error: %r" % e)  
        except Exception as e:  
            self._set_status("Error: %r" % e)  
            sys.print_exception(e)