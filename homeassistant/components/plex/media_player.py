"""Support to interface with the Plex API."""
import json
import logging
from xml.etree.ElementTree import ParseError

import plexapi.exceptions
import requests.exceptions

from homeassistant.components.media_player import MediaPlayerDevice
from homeassistant.components.media_player.const import (
    MEDIA_TYPE_MOVIE,
    MEDIA_TYPE_MUSIC,
    MEDIA_TYPE_TVSHOW,
    SUPPORT_NEXT_TRACK,
    SUPPORT_PAUSE,
    SUPPORT_PLAY,
    SUPPORT_PLAY_MEDIA,
    SUPPORT_PREVIOUS_TRACK,
    SUPPORT_STOP,
    SUPPORT_TURN_OFF,
    SUPPORT_VOLUME_MUTE,
    SUPPORT_VOLUME_SET,
)
from homeassistant.const import (
    DEVICE_DEFAULT_NAME,
    STATE_IDLE,
    STATE_OFF,
    STATE_PAUSED,
    STATE_PLAYING,
)
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util

from .const import (
    CONF_SERVER_IDENTIFIER,
    DISPATCHERS,
    DOMAIN as PLEX_DOMAIN,
    NAME_FORMAT,
    PLEX_NEW_MP_SIGNAL,
    PLEX_UPDATE_MEDIA_PLAYER_SIGNAL,
    SERVERS,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the Plex media_player platform.

    Deprecated.
    """
    pass


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up Plex media_player from a config entry."""
    server_id = config_entry.data[CONF_SERVER_IDENTIFIER]

    def async_new_media_players(new_entities):
        _async_add_entities(
            hass, config_entry, async_add_entities, server_id, new_entities
        )

    unsub = async_dispatcher_connect(hass, PLEX_NEW_MP_SIGNAL, async_new_media_players)
    hass.data[PLEX_DOMAIN][DISPATCHERS][server_id].append(unsub)


@callback
def _async_add_entities(
    hass, config_entry, async_add_entities, server_id, new_entities
):
    """Set up Plex media_player entities."""
    entities = []
    plexserver = hass.data[PLEX_DOMAIN][SERVERS][server_id]
    for entity_params in new_entities:
        plex_mp = PlexMediaPlayer(plexserver, **entity_params)
        entities.append(plex_mp)

    async_add_entities(entities, True)


class PlexMediaPlayer(MediaPlayerDevice):
    """Representation of a Plex device."""

    def __init__(self, plex_server, device, session=None):
        """Initialize the Plex device."""
        self.plex_server = plex_server
        self.device = device
        self.session = session
        self._app_name = ""
        self._available = False
        self._device_protocol_capabilities = None
        self._is_player_active = False
        self._machine_identifier = device.machineIdentifier
        self._make = ""
        self._name = None
        self._player_state = "idle"
        self._previous_volume_level = 1  # Used in fake muting
        self._session_type = None
        self._session_username = None
        self._state = STATE_IDLE
        self._volume_level = 1  # since we can't retrieve remotely
        self._volume_muted = False  # since we can't retrieve remotely
        # General
        self._media_content_id = None
        self._media_content_rating = None
        self._media_content_type = None
        self._media_duration = None
        self._media_image_url = None
        self._media_title = None
        self._media_position = None
        self._media_position_updated_at = None
        # Music
        self._media_album_artist = None
        self._media_album_name = None
        self._media_artist = None
        self._media_track = None
        # TV Show
        self._media_episode = None
        self._media_season = None
        self._media_series_title = None

    async def async_added_to_hass(self):
        """Run when about to be added to hass."""
        server_id = self.plex_server.machine_identifier
        unsub = async_dispatcher_connect(
            self.hass,
            PLEX_UPDATE_MEDIA_PLAYER_SIGNAL.format(self.unique_id),
            self.async_refresh_media_player,
        )
        self.hass.data[PLEX_DOMAIN][DISPATCHERS][server_id].append(unsub)

    @callback
    def async_refresh_media_player(self, device, session):
        """Set instance objects and trigger an entity state update."""
        self.device = device
        self.session = session
        self.async_schedule_update_ha_state(True)

    def _clear_media_details(self):
        """Set all Media Items to None."""
        # General
        self._media_content_id = None
        self._media_content_rating = None
        self._media_content_type = None
        self._media_duration = None
        self._media_image_url = None
        self._media_title = None
        # Music
        self._media_album_artist = None
        self._media_album_name = None
        self._media_artist = None
        self._media_track = None
        # TV Show
        self._media_episode = None
        self._media_season = None
        self._media_series_title = None

        # Clear library Name
        self._app_name = ""

    def update(self):
        """Refresh key device data."""
        self._clear_media_details()

        self._available = self.device or self.session
        name_base = None

        if self.device:
            try:
                device_url = self.device.url("/")
            except plexapi.exceptions.BadRequest:
                device_url = "127.0.0.1"
            if "127.0.0.1" in device_url:
                self.device.proxyThroughServer()
            name_base = self.device.title or self.device.product
            self._device_protocol_capabilities = self.device.protocolCapabilities
            self._player_state = self.device.state

        if not self.session:
            self.force_idle()
        else:
            session_device = next(
                (
                    p
                    for p in self.session.players
                    if p.machineIdentifier == self.device.machineIdentifier
                ),
                None,
            )
            if session_device:
                self._make = session_device.device or ""
                self._player_state = session_device.state
                name_base = name_base or session_device.title or session_device.product
            else:
                _LOGGER.warning("No player associated with active session")

            self._session_username = self.session.usernames[0]

            # Calculate throttled position for proper progress display.
            position = int(self.session.viewOffset / 1000)
            now = dt_util.utcnow()
            if self._media_position is not None:
                pos_diff = position - self._media_position
                time_diff = now - self._media_position_updated_at
                if pos_diff != 0 and abs(time_diff.total_seconds() - pos_diff) > 5:
                    self._media_position_updated_at = now
                    self._media_position = position
            else:
                self._media_position_updated_at = now
                self._media_position = position

            self._media_content_id = self.session.ratingKey
            self._media_content_rating = getattr(self.session, "contentRating", None)

        self._name = self._name or NAME_FORMAT.format(name_base or DEVICE_DEFAULT_NAME)
        self._set_player_state()

        if self._is_player_active and self.session is not None:
            self._session_type = self.session.type
            self._media_duration = int(self.session.duration / 1000)
            #  title (movie name, tv episode name, music song name)
            self._media_title = self.session.title
            # media type
            self._set_media_type()
            self._app_name = (
                self.session.section().title
                if self.session.section() is not None
                else ""
            )
            self._set_media_image()
        else:
            self._session_type = None

    def _set_media_image(self):
        thumb_url = self.session.thumbUrl
        if (
            self.media_content_type is MEDIA_TYPE_TVSHOW
            and not self.plex_server.use_episode_art
        ):
            thumb_url = self.session.url(self.session.grandparentThumb)

        if thumb_url is None:
            _LOGGER.debug(
                "Using media art because media thumb was not found: %s", self.name
            )
            thumb_url = self.session.url(self.session.art)

        self._media_image_url = thumb_url

    def _set_player_state(self):
        if self._player_state == "playing":
            self._is_player_active = True
            self._state = STATE_PLAYING
        elif self._player_state == "paused":
            self._is_player_active = True
            self._state = STATE_PAUSED
        elif self.device:
            self._is_player_active = False
            self._state = STATE_IDLE
        else:
            self._is_player_active = False
            self._state = STATE_OFF

    def _set_media_type(self):
        if self._session_type in ["clip", "episode"]:
            self._media_content_type = MEDIA_TYPE_TVSHOW

            # season number (00)
            if callable(self.session.season):
                self._media_season = str((self.session.season()).index).zfill(2)
            elif self.session.parentIndex is not None:
                self._media_season = self.session.parentIndex.zfill(2)
            else:
                self._media_season = None
            # show name
            self._media_series_title = self.session.grandparentTitle
            # episode number (00)
            if self.session.index is not None:
                self._media_episode = str(self.session.index).zfill(2)

        elif self._session_type == "movie":
            self._media_content_type = MEDIA_TYPE_MOVIE
            if self.session.year is not None and self._media_title is not None:
                self._media_title += " (" + str(self.session.year) + ")"

        elif self._session_type == "track":
            self._media_content_type = MEDIA_TYPE_MUSIC
            self._media_album_name = self.session.parentTitle
            self._media_album_artist = self.session.grandparentTitle
            self._media_track = self.session.index
            self._media_artist = self.session.originalTitle
            # use album artist if track artist is missing
            if self._media_artist is None:
                _LOGGER.debug(
                    "Using album artist because track artist was not found: %s",
                    self.name,
                )
                self._media_artist = self._media_album_artist

    def force_idle(self):
        """Force client to idle."""
        self._state = STATE_IDLE
        self.session = None
        self._clear_media_details()

    @property
    def should_poll(self):
        """Return True if entity has to be polled for state."""
        return False

    @property
    def unique_id(self):
        """Return the id of this plex client."""
        return self._machine_identifier

    @property
    def available(self):
        """Return the availability of the client."""
        return self._available

    @property
    def name(self):
        """Return the name of the device."""
        return self._name

    @property
    def app_name(self):
        """Return the library name of playing media."""
        return self._app_name

    @property
    def state(self):
        """Return the state of the device."""
        return self._state

    @property
    def _active_media_plexapi_type(self):
        """Get the active media type required by PlexAPI commands."""
        if self.media_content_type is MEDIA_TYPE_MUSIC:
            return "music"

        return "video"

    @property
    def media_content_id(self):
        """Return the content ID of current playing media."""
        return self._media_content_id

    @property
    def media_content_type(self):
        """Return the content type of current playing media."""
        if self._session_type == "clip":
            _LOGGER.debug(
                "Clip content type detected, compatibility may vary: %s", self.name
            )
            return MEDIA_TYPE_TVSHOW
        if self._session_type == "episode":
            return MEDIA_TYPE_TVSHOW
        if self._session_type == "movie":
            return MEDIA_TYPE_MOVIE
        if self._session_type == "track":
            return MEDIA_TYPE_MUSIC

        return None

    @property
    def media_artist(self):
        """Return the artist of current playing media, music track only."""
        return self._media_artist

    @property
    def media_album_name(self):
        """Return the album name of current playing media, music track only."""
        return self._media_album_name

    @property
    def media_album_artist(self):
        """Return the album artist of current playing media, music only."""
        return self._media_album_artist

    @property
    def media_track(self):
        """Return the track number of current playing media, music only."""
        return self._media_track

    @property
    def media_duration(self):
        """Return the duration of current playing media in seconds."""
        return self._media_duration

    @property
    def media_position(self):
        """Return the duration of current playing media in seconds."""
        return self._media_position

    @property
    def media_position_updated_at(self):
        """When was the position of the current playing media valid."""
        return self._media_position_updated_at

    @property
    def media_image_url(self):
        """Return the image URL of current playing media."""
        return self._media_image_url

    @property
    def media_title(self):
        """Return the title of current playing media."""
        return self._media_title

    @property
    def media_season(self):
        """Return the season of current playing media (TV Show only)."""
        return self._media_season

    @property
    def media_series_title(self):
        """Return the title of the series of current playing media."""
        return self._media_series_title

    @property
    def media_episode(self):
        """Return the episode of current playing media (TV Show only)."""
        return self._media_episode

    @property
    def make(self):
        """Return the make of the device (ex. SHIELD Android TV)."""
        return self._make

    @property
    def supported_features(self):
        """Flag media player features that are supported."""
        # force show all controls
        if self.plex_server.show_all_controls:
            return (
                SUPPORT_PAUSE
                | SUPPORT_PREVIOUS_TRACK
                | SUPPORT_NEXT_TRACK
                | SUPPORT_STOP
                | SUPPORT_VOLUME_SET
                | SUPPORT_PLAY
                | SUPPORT_PLAY_MEDIA
                | SUPPORT_TURN_OFF
                | SUPPORT_VOLUME_MUTE
            )

        # no mute support
        if self.make.lower() == "shield android tv":
            _LOGGER.debug(
                "Shield Android TV client detected, disabling mute controls: %s",
                self.name,
            )
            return (
                SUPPORT_PAUSE
                | SUPPORT_PREVIOUS_TRACK
                | SUPPORT_NEXT_TRACK
                | SUPPORT_STOP
                | SUPPORT_VOLUME_SET
                | SUPPORT_PLAY
                | SUPPORT_PLAY_MEDIA
                | SUPPORT_TURN_OFF
            )

        # Only supports play,pause,stop (and off which really is stop)
        if self.make.lower().startswith("tivo"):
            _LOGGER.debug(
                "Tivo client detected, only enabling pause, play, "
                "stop, and off controls: %s",
                self.name,
            )
            return SUPPORT_PAUSE | SUPPORT_PLAY | SUPPORT_STOP | SUPPORT_TURN_OFF

        if self.device and "playback" in self._device_protocol_capabilities:
            return (
                SUPPORT_PAUSE
                | SUPPORT_PREVIOUS_TRACK
                | SUPPORT_NEXT_TRACK
                | SUPPORT_STOP
                | SUPPORT_VOLUME_SET
                | SUPPORT_PLAY
                | SUPPORT_PLAY_MEDIA
                | SUPPORT_TURN_OFF
                | SUPPORT_VOLUME_MUTE
            )

        return 0

    def set_volume_level(self, volume):
        """Set volume level, range 0..1."""
        if self.device and "playback" in self._device_protocol_capabilities:
            self.device.setVolume(int(volume * 100), self._active_media_plexapi_type)
            self._volume_level = volume  # store since we can't retrieve
            self.plex_server.update_platforms()

    @property
    def volume_level(self):
        """Return the volume level of the client (0..1)."""
        if (
            self._is_player_active
            and self.device
            and "playback" in self._device_protocol_capabilities
        ):
            return self._volume_level

    @property
    def is_volume_muted(self):
        """Return boolean if volume is currently muted."""
        if self._is_player_active and self.device:
            return self._volume_muted

    def mute_volume(self, mute):
        """Mute the volume.

        Since we can't actually mute, we'll:
        - On mute, store volume and set volume to 0
        - On unmute, set volume to previously stored volume
        """
        if not (self.device and "playback" in self._device_protocol_capabilities):
            return

        self._volume_muted = mute
        if mute:
            self._previous_volume_level = self._volume_level
            self.set_volume_level(0)
        else:
            self.set_volume_level(self._previous_volume_level)

    def media_play(self):
        """Send play command."""
        if self.device and "playback" in self._device_protocol_capabilities:
            self.device.play(self._active_media_plexapi_type)
            self.plex_server.update_platforms()

    def media_pause(self):
        """Send pause command."""
        if self.device and "playback" in self._device_protocol_capabilities:
            self.device.pause(self._active_media_plexapi_type)
            self.plex_server.update_platforms()

    def media_stop(self):
        """Send stop command."""
        if self.device and "playback" in self._device_protocol_capabilities:
            self.device.stop(self._active_media_plexapi_type)
            self.plex_server.update_platforms()

    def turn_off(self):
        """Turn the client off."""
        # Fake it since we can't turn the client off
        self.media_stop()

    def media_next_track(self):
        """Send next track command."""
        if self.device and "playback" in self._device_protocol_capabilities:
            self.device.skipNext(self._active_media_plexapi_type)
            self.plex_server.update_platforms()

    def media_previous_track(self):
        """Send previous track command."""
        if self.device and "playback" in self._device_protocol_capabilities:
            self.device.skipPrevious(self._active_media_plexapi_type)
            self.plex_server.update_platforms()

    def play_media(self, media_type, media_id, **kwargs):
        """Play a piece of media."""
        if not (self.device and "playback" in self._device_protocol_capabilities):
            return

        src = json.loads(media_id)
        library = src.get("library_name")
        shuffle = src.get("shuffle", 0)

        media = None

        if media_type == "MUSIC":
            media = self._get_music_media(library, src)
        elif media_type == "EPISODE":
            media = self._get_tv_media(library, src)
        elif media_type == "PLAYLIST":
            media = self.plex_server.playlist(src["playlist_name"])
        elif media_type == "VIDEO":
            media = self.plex_server.library.section(library).get(src["video_name"])

        if media is None:
            _LOGGER.error("Media could not be found: %s", media_id)
            return

        playqueue = self.plex_server.create_playqueue(media, shuffle=shuffle)
        try:
            self.device.playMedia(playqueue)
        except ParseError:
            # Temporary workaround for Plexamp / plexapi issue
            pass
        except requests.exceptions.ConnectTimeout:
            _LOGGER.error("Timed out playing on %s", self.name)

        self.plex_server.update_platforms()

    def _get_music_media(self, library_name, src):
        """Find music media and return a Plex media object."""
        artist_name = src["artist_name"]
        album_name = src.get("album_name")
        track_name = src.get("track_name")
        track_number = src.get("track_number")

        artist = self.plex_server.library.section(library_name).get(artist_name)

        if album_name:
            album = artist.album(album_name)

            if track_name:
                return album.track(track_name)

            if track_number:
                for track in album.tracks():
                    if int(track.index) == int(track_number):
                        return track
                return None

            return album

        if track_name:
            return artist.searchTracks(track_name, maxresults=1)
        return artist

    def _get_tv_media(self, library_name, src):
        """Find TV media and return a Plex media object."""
        show_name = src["show_name"]
        season_number = src.get("season_number")
        episode_number = src.get("episode_number")
        target_season = None
        target_episode = None

        show = self.plex_server.library.section(library_name).get(show_name)

        if not season_number:
            return show

        for season in show.seasons():
            if int(season.seasonNumber) == int(season_number):
                target_season = season
                break

        if target_season is None:
            _LOGGER.error(
                "Season not found: %s\\%s - S%sE%s",
                library_name,
                show_name,
                str(season_number).zfill(2),
                str(episode_number).zfill(2),
            )
        else:
            if not episode_number:
                return target_season

            for episode in target_season.episodes():
                if int(episode.index) == int(episode_number):
                    target_episode = episode
                    break

            if target_episode is None:
                _LOGGER.error(
                    "Episode not found: %s\\%s - S%sE%s",
                    library_name,
                    show_name,
                    str(season_number).zfill(2),
                    str(episode_number).zfill(2),
                )

        return target_episode

    @property
    def device_state_attributes(self):
        """Return the scene state attributes."""
        attr = {
            "media_content_rating": self._media_content_rating,
            "session_username": self._session_username,
            "media_library_name": self._app_name,
        }

        return attr
