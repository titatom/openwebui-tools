"""  
title: Radarr & Sonarr Manager  
author: you  
version: 0.4.0  
required_open_webui_version: 0.4.0  
requirements: requests  
"""  
  
import base64  
import json  
import requests  
from pydantic import BaseModel, Field  
  
  
class Tools:  
    class Valves(BaseModel):  
        RADARR_URL: str = Field(default="http://localhost:7878", description="Base URL for Radarr")  
        RADARR_API_KEY: str = Field(default="", description="Radarr API key")  
        SONARR_URL: str = Field(default="http://localhost:8989", description="Base URL for Sonarr")  
        SONARR_API_KEY: str = Field(default="", description="Sonarr API key")  
        REQUEST_TIMEOUT: int = Field(default=20, description="Default HTTP timeout (seconds)")  
        SEARCH_TIMEOUT: int = Field(default=90, description="Timeout for live indexer searches (seconds)")  
  
    def __init__(self):  
        self.valves = self.Valves()  
  
    # ---------- internal helpers ----------  
    def _endpoint(self, service: str):  
        s = (service or "").strip().lower()  
        if s == "radarr":  
            return self.valves.RADARR_URL, self.valves.RADARR_API_KEY  
        if s == "sonarr":  
            return self.valves.SONARR_URL, self.valves.SONARR_API_KEY  
        raise ValueError("service must be 'radarr' or 'sonarr'")  
  
    def _req(self, service, method, path, timeout=None, **kwargs):  
        base, key = self._endpoint(service)  
        if not base or not key:  
            raise RuntimeError(f"{service} URL or API key not configured in Valves.")  
        url = f"{base.rstrip('/')}/api/v3/{path.lstrip('/')}"  
        headers = {"X-Api-Key": key}  
        try:  
            resp = requests.request(  
                method, url, headers=headers,  
                timeout=timeout or self.valves.REQUEST_TIMEOUT,** kwargs,  
            )  
            resp.raise_for_status()  
        except requests.exceptions.ConnectionError:  
            raise RuntimeError(f"Could not reach {service} at {base}. Check URL/network.")  
        except requests.exceptions.Timeout:  
            raise RuntimeError(f"{service} request timed out.")  
        except requests.exceptions.HTTPError as e:  
            raise RuntimeError(f"{service} API error: {e} — {resp.text[:200]}")  
        return resp.json() if resp.content else {}  
  
    @staticmethod  
    def _encode_token(service, guid, indexer_id, rejected=False, rejections=None):  
        raw = json.dumps({  
            "s": service, "g": guid, "i": indexer_id,  
            "r": bool(rejected),  
            "rj": (rejections or [])[:5],  
        })  
        return base64.urlsafe_b64encode(raw.encode()).decode()  
  
    @staticmethod  
    def _decode_token(token):  
        raw = base64.urlsafe_b64decode(token.encode()).decode()  
        return json.loads(raw)  
  
    # ---------- library listing ----------  
    def list_library(self, service: str) -> str:  
        """  
        List the current movie (radarr) or series (sonarr) library.  
        :param service: Either 'radarr' or 'sonarr'.  
        :return: A list of titles in the library.  
        """  
        path = "movie" if service.lower() == "radarr" else "series"  
        items = self._req(service, "GET", path)  
        if not items:  
            return f"{service} library is empty."  
        lines = [f"- {it.get('title')} ({it.get('year','?')})" for it in items[:50]]  
        more = f"\n…and {len(items)-50} more" if len(items) > 50 else ""  
        return "\n".join(lines) + more  
  
    # ---------- search to add ----------  
    def search_to_add(self, service: str, term: str) -> str:  
        """  
        Search for a movie (radarr) or series (sonarr) by title to find candidates to add.  
        :param service: Either 'radarr' or 'sonarr'.  
        :param term: The title to search for.  
        :return: A list of matches with the id needed to add them (tmdbId or tvdbId).  
        """  
        path = "movie/lookup" if service.lower() == "radarr" else "series/lookup"  
        results = self._req(service, "GET", path, params={"term": term})  
        if not results:  
            return f"No results for '{term}'."  
        id_key = "tmdbId" if service.lower() == "radarr" else "tvdbId"  
        lines = [  
            f"- {r.get('title')} ({r.get('year','?')}) [{id_key}={r.get(id_key)}]"  
            for r in results[:10]  
        ]  
        return "\n".join(lines)  
  
    # ---------- add to library ----------  
    def add_item(self, service: str, item_id: int) -> str:  
        """  
        Add a movie (by tmdbId) or series (by tvdbId) to the library and start searching.  
        :param service: Either 'radarr' or 'sonarr'.  
        :param item_id: tmdbId for radarr, tvdbId for sonarr (from search_to_add).  
        :return: Confirmation message.  
        """  
        svc = service.lower()  
        profiles = self._req(service, "GET", "qualityprofile")  
        roots = self._req(service, "GET", "rootfolder")  
        if svc == "radarr":  
            lookup = self._req(service, "GET", "movie/lookup/tmdb", params={"tmdbId": item_id})  
            payload = {  
                **lookup,  
                "qualityProfileId": profiles[0]["id"],  
                "rootFolderPath": roots[0]["path"],  
                "monitored": True,  
                "addOptions": {"searchForMovie": True},  
            }  
            self._req(service, "POST", "movie", json=payload)  
        else:  
            lookup = self._req(service, "GET", "series/lookup", params={"term": f"tvdb:{item_id}"})  
            lookup = lookup[0] if isinstance(lookup, list) else lookup  
            payload = {**                lookup,  
                "qualityProfileId": profiles[0]["id"],  
                "rootFolderPath": roots[0]["path"],  
                "monitored": True,  
                "addOptions": {"searchForMissingEpisodes": True},  
            }  
            self._req(service, "POST", "series", json=payload)  
        return f"Added '{lookup.get('title')}' to {service} and started search."  
  
    # ---------- missing items ----------  
    def get_missing(self, service: str, page_size: int = 20) -> str:  
        """  
        List monitored items missing files: episodes (sonarr) or movies (radarr).  
        :param service: Either 'radarr' or 'sonarr'.  
        :param page_size: Number of items to return (default 20).  
        :return: Numbered list with the id needed for interactive_search.  
        """  
        params = {  
            "page": 1, "pageSize": page_size,  
            "sortKey": "airDateUtc" if service.lower() == "sonarr" else "title",  
            "sortDirection": "descending",  
        }  
        if service.lower() == "sonarr":  
            params["includeSeries"] = "true"  
        data = self._req(service, "GET", "wanted/missing", params=params)  
        records = data.get("records", [])  
        if not records:  
            return "No missing items found. 🎉"  
        lines = []  
        for r in records:  
            if service.lower() == "sonarr":  
                series = r.get("series", {}).get("title", "Unknown")  
                code = f"S{r['seasonNumber']:02d}E{r['episodeNumber']:02d}"  
                lines.append(f"- episodeId={r['id']} | {series} {code} - {r.get('title','')}")  
            else:  
                lines.append(f"- movieId={r['id']} | {r.get('title')} ({r.get('year','?')})")  
        return "\n".join(lines)  
  
    # ---------- interactive search ----------  
    async def interactive_search(self, service: str, item_id: int, __event_emitter__=None) -> str:  
        """  
        Run an interactive (manual) search across indexers for a specific episode (sonarr)  
        or movie (radarr). Returns the available releases WITHOUT downloading. Each release  
        includes a token to pass to grab_release.  
        :param service: Either 'radarr' or 'sonarr'.  
        :param item_id: episodeId for sonarr, movieId for radarr (from get_missing).  
        :return: Numbered list of releases with quality, size, seeders, indexer, and a grab token.  
        """  
        if __event_emitter__:  
            await __event_emitter__({"type": "status",  
                "data": {"description": "Searching indexers… (this can take up to a minute)", "done": False}})  
  
        param_key = "episodeId" if service.lower() == "sonarr" else "movieId"  
        results = self._req(service, "GET", "release",  
                            params={param_key: item_id}, timeout=self.valves.SEARCH_TIMEOUT)  
  
        if __event_emitter__:  
            await __event_emitter__({"type": "status",  
                "data": {"description": f"Found {len(results)} releases", "done": True}})  
  
        if not results:  
            return "No releases found from any indexer."  
  
        results.sort(key=lambda r: (bool(r.get("rejected")), -(r.get("seeders", 0) or 0)))  
  
        lines = []  
        for r in results[:15]:  
            size_gb = round(r.get("size", 0) / (1024 **3), 2)  
            quality = r.get("quality", {}).get("quality", {}).get("name", "?")  
            seeders = r.get("seeders", "—")  
            indexer = r.get("indexer", "?")  
            status = ("❌ " + "; ".join(r.get("rejections", []))) if r.get("rejected") else "✅ approved"  
            token = self._encode_token(  
                service.lower(), r["guid"], r["indexerId"],  
                rejected=bool(r.get("rejected")),  
                rejections=r.get("rejections", []),  
            )  
            lines.append(  
                f"{quality} | {size_gb} GB | seeders={seeders} | {indexer} | {status}\n"  
                f"  {r.get('title','')}\n  token={token}"  
            )  
        return ("Releases (call grab_release with the chosen token):\n\n" + "\n\n".join(lines))  
  
    # ---------- find seriesId (Sonarr) ----------  
    def find_series_id(self, title: str) -> str:  
        """  
        Find the Sonarr seriesId and season list for an already-added series by title.  
        Use this before season_pack_search or set_season_monitored.  
        :param title: The series title (partial match ok).  
        :return: Matching series with seriesId and their seasons (number + completion).  
        """  
        series = self._req("sonarr", "GET", "series")  
        matches = [s for s in series if title.lower() in s.get("title", "").lower()]  
        if not matches:  
            return f"No added series match '{title}'. Add it first with add_item."  
        out = []  
        for s in matches[:5]:  
            lines = [f"seriesId={s['id']} | {s['title']} ({s.get('year','?')})"]  
            for season in s.get("seasons", []):  
                stats = season.get("statistics", {})  
                have = stats.get("episodeFileCount", 0)  
                total = stats.get("totalEpisodeCount", 0)  
                mon = "monitored" if season.get("monitored") else "unmonitored"  
                lines.append(  
                    f"   season {season['seasonNumber']}: "  
                    f"{have}/{total} episodes | {mon}"  
                )  
            out.append("\n".join(lines))  
        return "\n\n".join(out)  
  
    # ---------- season-pack search (Sonarr) ----------  
    async def season_pack_search(  
        self, series_id: int, season_number: int, packs_only: bool = True,  
        __event_emitter__=None,  
    ) -> str:  
        """  
        Interactive search across indexers for a whole season in Sonarr. Returns available  
        releases (season packs and optionally individual episodes) WITHOUT downloading.  
        Each release includes a token for grab_release.  
        :param series_id: The Sonarr seriesId (from find_series_id).  
        :param season_number: The season number to search for.  
        :param packs_only: If true, only show full-season packs; if false, include singles.  
        :return: Numbered list of releases with quality, size, seeders, indexer, and a grab token.  
        """  
        if __event_emitter__:  
            await __event_emitter__({"type": "status",  
                "data": {"description": f"Searching indexers for season {season_number}…", "done": False}})  
  
        results = self._req(  
            "sonarr", "GET", "release",  
            params={"seriesId": series_id, "seasonNumber": season_number},  
            timeout=self.valves.SEARCH_TIMEOUT,  
        )  
  
        if packs_only:  
            results = [r for r in results if r.get("fullSeason")]  
  
        if __event_emitter__:  
            await __event_emitter__({"type": "status",  
                "data": {"description": f"Found {len(results)} releases", "done": True}})  
  
        if not results:  
            kind = "season packs" if packs_only else "releases"  
            return f"No {kind} found for season {season_number}."  
  
        results.sort(key=lambda r: (  
            bool(r.get("rejected")),  
            not r.get("fullSeason"),  
            -(r.get("seeders", 0) or 0),  
        ))  
  
        lines = []  
        for r in results[:15]:  
            size_gb = round(r.get("size", 0) / (1024** 3), 2)  
            quality = r.get("quality", {}).get("quality", {}).get("name", "?")  
            seeders = r.get("seeders", "—")  
            indexer = r.get("indexer", "?")  
            pack = "📦 PACK" if r.get("fullSeason") else "episode"  
            status = ("❌ " + "; ".join(r.get("rejections", []))) if r.get("rejected") else "✅ approved"  
            token = self._encode_token(  
                "sonarr", r["guid"], r["indexerId"],  
                rejected=bool(r.get("rejected")),  
                rejections=r.get("rejections", []),  
            )  
            lines.append(  
                f"{pack} | {quality} | {size_gb} GB | seeders={seeders} | {indexer} | {status}\n"  
                f"  {r.get('title','')}\n  token={token}"  
            )  
        return ("Season releases (call grab_release with the chosen token):\n\n"  
                + "\n\n".join(lines))  
  
    # ---------- season monitoring (Sonarr) ----------  
    def set_season_monitored(  
        self, series_id: int, season_number: int, monitored: bool = True  
    ) -> str:  
        """  
        Set whether a specific season of a Sonarr series is monitored. Monitoring lets  
        Sonarr automatically manage and search for that season's episodes.  
        :param series_id: The Sonarr seriesId (from find_series_id).  
        :param season_number: The season number to update.  
        :param monitored: True to monitor, False to unmonitor (default True).  
        :return: Confirmation of the new monitoring state.  
        """  
        series = self._req("sonarr", "GET", f"series/{series_id}")  
        found = False  
        for season in series.get("seasons", []):  
            if season.get("seasonNumber") == season_number:  
                season["monitored"] = monitored  
                found = True  
                break  
        if not found:  
            return f"Season {season_number} not found for seriesId={series_id}."  
        self._req("sonarr", "PUT", f"series/{series_id}", json=series)  
        state = "monitored" if monitored else "unmonitored"  
        return f"Season {season_number} of '{series.get('title')}' is now {state}."  
  
    # ---------- grab (stateless, with rejection guard) ----------  
    def grab_release(self, token: str, confirm_rejected: bool = False) -> str:  
        """  
        Download a chosen release using the token from interactive_search or  
        season_pack_search. If the release was REJECTED by quality/profile rules,  
        this returns a warning and will NOT download unless confirm_rejected=True.  
        :param token: The token string shown next to a release.  
        :param confirm_rejected: Set True only after the user confirms they want a rejected release.  
        :return: Confirmation, or a warning that requires re-calling with confirm_rejected=True.  
        """  
        try:  
            data = self._decode_token(token)  
        except Exception:  
            return "Invalid token. Re-run the search and copy a token exactly."  
  
        if data.get("r") and not confirm_rejected:  
            reasons = data.get("rj", []) or ["(no reason provided)"]  
            reason_text = "; ".join(reasons)  
            return (  
                "⚠️ This release was REJECTED by Sonarr/Radarr quality rules:\n"  
                f"   {reason_text}\n\n"  
                "Grabbing it overrides those rules. If the user still wants it, call "  
                "grab_release again with the SAME token and confirm_rejected=True."  
            )  
  
        payload = {"guid": data["g"], "indexerId": data["i"]}  
        self._req(data["s"], "POST", "release", json=payload)  
        prefix = "⚠️ Force-grabbed rejected release. " if data.get("r") else ""  
        return f"{prefix}Sent to download client ✅"  
  
    # ---------- status ----------  
    def get_queue(self, service: str) -> str:  
        """  
        Show the current download queue for radarr or sonarr.  
        :param service: Either 'radarr' or 'sonarr'.  
        :return: List of active downloads with progress.  
        """  
        data = self._req(service, "GET", "queue", params={"pageSize": 50})  
        records = data.get("records", data if isinstance(data, list) else [])  
        if not records:  
            return "Download queue is empty."  
        lines = []  
        for q in records:  
            title = q.get("title", "?")  
            status = q.get("status", "?")  
            tl = q.get("timeleft", "?")  
            lines.append(f"- {title} | {status} | {tl} left")  
        return "\n".join(lines)  
  
    def disk_space(self, service: str) -> str:  
        """  
        Report free disk space known to radarr or sonarr.  
        :param service: Either 'radarr' or 'sonarr'.  
        :return: Free/total space per root path.  
        """  
        data = self._req(service, "GET", "diskspace")  
        lines = []  
        for d in data:  
            free = round(d.get("freeSpace", 0) / (1024 **3), 1)  
            total = round(d.get("totalSpace", 0) / (1024** 3), 1)  
            lines.append(f"- {d.get('path')}: {free} GB free of {total} GB")  
        return "\n".join(lines) if lines else "No disk info available."