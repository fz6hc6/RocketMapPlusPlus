#!/usr/bin/python
# -*- coding: utf-8 -*-

import calendar
import logging
import gc
import os
import math

import time
from datetime import datetime, timedelta
from s2sphere import LatLng
from bisect import bisect_left
from flask import Flask, abort, jsonify, render_template, request,\
    make_response, send_from_directory, json, send_file, redirect, session
from flask.json import JSONEncoder
from flask_compress import Compress
from pogom.transform import jitter_location
from pogom.dyn_img import get_gym_icon
from pogom.weather import get_weather_cells, get_s2_coverage, get_weather_alerts
from base64 import b64decode

import gpxpy

import s2sphere
from peewee import DeleteQuery

from .userAuth import DiscordAPI
from .models import (Pokemon, Gym, GymDetails, Pokestop, Raid, ScannedLocation,
                     MainWorker, WorkerStatus, Token,
                     SpawnPoint, DeviceWorker, SpawnpointDetectionData, ScanSpawnPoint, PokestopMember,
                     Quest, PokestopDetails, GymMember, GymPokemon, Weather)
from .utils import (get_args, get_pokemon_name, get_pokemon_types,
                    now, dottedQuadToNum, date_secs, calc_pokemon_level,
                    get_timezone_offset)
from .transform import transform_from_wgs_to_gcj
from .blacklist import fingerprints, get_ip_blacklist
from .customLog import printPokemon
import re
import json
from werkzeug.datastructures import MultiDict

import geopy

from google.protobuf.json_format import MessageToJson
from protos.pogoprotos.networking.responses.fort_search_response_pb2 import FortSearchResponse
from protos.pogoprotos.networking.responses.encounter_response_pb2 import EncounterResponse
from protos.pogoprotos.networking.responses.get_map_objects_response_pb2 import GetMapObjectsResponse, _GETMAPOBJECTSRESPONSE_TIMEOFDAY
from protos.pogoprotos.networking.responses.gym_get_info_response_pb2 import GymGetInfoResponse
from protos.pogoprotos.networking.responses.fort_details_response_pb2 import FortDetailsResponse
from protos.pogoprotos.networking.responses.get_player_response_pb2 import GetPlayerResponse
from protos.pogoprotos.map.weather.display_weather_pb2 import _DISPLAYWEATHER_DISPLAYLEVEL
from protos.pogoprotos.map.weather.gameplay_weather_pb2 import _GAMEPLAYWEATHER_WEATHERCONDITION
from protos.pogoprotos.map.weather.weather_alert_pb2 import _WEATHERALERT_SEVERITY

from protos.pogoprotos.enums.team_color_pb2 import _TEAMCOLOR
from protos.pogoprotos.enums.pokemon_id_pb2 import _POKEMONID
from protos.pogoprotos.enums.pokemon_move_pb2 import _POKEMONMOVE
from protos.pogoprotos.enums.raid_level_pb2 import _RAIDLEVEL
from protos.pogoprotos.enums.gender_pb2 import _GENDER
from protos.pogoprotos.enums.form_pb2 import _FORM
from protos.pogoprotos.enums.costume_pb2 import _COSTUME
from protos.pogoprotos.enums.weather_condition_pb2 import _WEATHERCONDITION
from protos.pogoprotos.enums.quest_type_pb2 import _QUESTTYPE
from protos.pogoprotos.data.quests.quest_reward_pb2 import _QUESTREWARD_TYPE
from protos.pogoprotos.inventory.item.item_id_pb2 import _ITEMID
from protos.pogoprotos.data.quests.quest_condition_pb2 import _QUESTCONDITION_CONDITIONTYPE
from protos.pogoprotos.enums.pokemon_type_pb2 import _POKEMONTYPE
from protos.pogoprotos.enums.activity_type_pb2 import _ACTIVITYTYPE

log = logging.getLogger(__name__)
compress = Compress()


def convert_pokemon_list(pokemon):
    args = get_args()
    # Performance:  disable the garbage collector prior to creating a
    # (potentially) large dict with append().
    gc.disable()

    pokemon_result = []
    for p in pokemon:
        p['pokemon_name'] = get_pokemon_name(p['pokemon_id'])
        p['pokemon_types'] = get_pokemon_types(p['pokemon_id'])
        p['encounter_id'] = str(p['encounter_id'])
        if p['cp'] is None:
            p['cp'] = 0
        if args.china:
            p['latitude'], p['longitude'] = \
                transform_from_wgs_to_gcj(p['latitude'], p['longitude'])
        pokemon_result.append(p)

    # Re-enable the GC.
    gc.enable()
    return pokemon


class Pogom(Flask):

    def __init__(self, import_name, **kwargs):
        self.db_update_queue = kwargs.get('db_update_queue')
        kwargs.pop('db_update_queue')
        self.wh_update_queue = kwargs.get('wh_update_queue')
        kwargs.pop('wh_update_queue')
        super(Pogom, self).__init__(import_name, **kwargs)
        compress.init_app(self)

        args = get_args()

        # Global blist
        if not args.disable_blacklist:
            log.info('Retrieving blacklist...')
            self.blacklist = get_ip_blacklist()
            # Sort & index for binary search
            self.blacklist.sort(key=lambda r: r[0])
            self.blacklist_keys = [
                dottedQuadToNum(r[0]) for r in self.blacklist
            ]
        else:
            log.info('Blacklist disabled for this session.')
            self.blacklist = []
            self.blacklist_keys = []

        if args.user_auth:
            # Setup user authentication
            self.discord_api = DiscordAPI(args)
            self.secret_key = args.user_auth_secret_key
            self.permanent_session_lifetime = 7 * 24 * 3600

        # Routes
        self.json_encoder = CustomJSONEncoder
        self.route("/", methods=['GET'])(self.fullmap)
        self.route("/raids", methods=['GET'])(self.raidview)
        self.route("/quests", methods=['GET'])(self.questview)
        self.route("/auth_callback", methods=['GET'])(self.auth_callback)
        self.route("/auth_logout", methods=['GET'])(self.auth_logout)
        self.route("/raw_data", methods=['GET'])(self.raw_data)
        self.route("/raw_raid", methods=['POST'])(self.raw_raid)
        self.route("/raw_quests", methods=['POST'])(self.raw_quests)

        self.route("/loc", methods=['GET'])(self.loc)

        if not args.map_only:
            self.route("/devices", methods=['GET'])(self.devicesview)
            self.route("/raw_devices", methods=['POST'])(self.raw_devices)
            self.route("/webhook", methods=['GET', 'POST'])(self.webhook)
            self.route("/walk_spawnpoint", methods=['GET', 'POST'])(self.old_walk_spawnpoint)
            self.route("/walk_gpx", methods=['GET', 'POST'])(self.old_walk_gpx)
            self.route("/walk_pokestop", methods=['GET', 'POST'])(self.old_walk_pokestop)
            self.route("/teleport_gym", methods=['GET', 'POST'])(self.old_teleport_gym)
            self.route("/teleport_gpx", methods=['GET', 'POST'])(self.old_teleport_gpx)
            self.route("/scan_loc", methods=['GET', 'POST'])(self.old_scan_loc)
            self.route("/mapcontrolled", methods=['GET', 'POST'])(self.old_mapcontrolled)
            self.route("/loc/<endpoint>", methods=['GET', 'POST'])(self.unifiedEndpoints)
            self.route("/next_loc", methods=['POST'])(self.next_loc)
            self.route("/new_endpoint", methods=['POST'])(self.new_endpoint)

        self.route("/new_name", methods=['POST'])(self.new_name)
        self.route("/new_username", methods=['POST'])(self.new_username)

        self.route("/mobile", methods=['GET'])(self.list_pokemon)
        self.route("/search_control", methods=['GET'])(self.get_search_control)
        self.route("/search_control", methods=['POST'])(
            self.post_search_control)
        self.route("/stats", methods=['GET'])(self.get_stats)
        self.route("/gym_data", methods=['GET'])(self.get_gymdata)
        self.route("/pokestop_data", methods=['GET'])(self.get_pokestopdata)
        self.route("/get_deviceworkerdata", methods=['GET'])(self.get_deviceworkerdata)
        self.route("/submit_token", methods=['POST'])(self.submit_token)
        self.route("/robots.txt", methods=['GET'])(self.render_robots_txt)
        self.route("/serviceWorker.min.js", methods=['GET'])(
            self.render_service_worker_js)
        self.route("/feedpokemon", methods=['GET'])(self.feedpokemon)
        self.route("/feedquest", methods=['GET'])(self.feedquest)
        self.route("/gym_img", methods=['GET'])(self.gym_img)

        self.deviceschedules = {}
        self.devicesscheduling = []
        self.devices = {}
        self.deviceschecked = None
        self.trusteddevices = {}
        self.devicessavetime = {}

        self.devices_last_scanned_times = {}
        self.devices_last_teleport_time = {}
        self.geofences = None
        self.devices_users = {}
        self.ga_alerts = {}

    def get_active_devices(self):
        result = []

        for uuid, dev in self.devices.iteritems():
            device = self.get_device(uuid, dev['latitude'], dev['longitude'])
            last_updated = device['last_updated']
            difference = (datetime.utcnow() - last_updated).total_seconds()
            if difference > 300 and device['fetching'] != 'IDLE':
                device['fetching'] = 'IDLE'
            last_scanned = device['last_scanned']
            if last_scanned is None and device['scanning'] != -1:
                device['scanning'] = -1
            else:
                difference = (datetime.utcnow() - last_scanned).total_seconds()
                if difference > 60 and device['scanning'] == 1:
                    device['scanning'] = 0

            if device['scanning'] == 1 or device['fetching'] != 'IDLE':
                result.append(device)

        return result

    def get_device(self, uuid, lat, lng):
        if uuid not in self.devices:
            self.devices[uuid] = DeviceWorker.get_by_id(uuid, lat, lng)
            self.devices[uuid]['route'] = ''
            self.devices[uuid]['no_overlap'] = False
            self.devices[uuid]['mapcontrolled'] = False
        device = self.devices[uuid].copy()

        last_updated = device['last_updated']
        last_scanned = device['last_scanned']

        difference = (datetime.utcnow() - last_updated).total_seconds()
        if last_scanned is None:
            difference2 = 60
        else:
            difference2 = (datetime.utcnow() - last_scanned).total_seconds()
        if difference > 30 and difference2 > 30:
            route = self.devices[uuid].get('route', '')
            no_overlap = self.devices[uuid].get('no_overlap', False)
            mapcontrolled = self.devices[uuid].get('mapcontrolled', False)
            self.devices[uuid] = DeviceWorker.get_by_id(uuid, lat, lng)
            self.devices[uuid]['route'] = route
            self.devices[uuid]['no_overlap'] = no_overlap
            self.devices[uuid]['mapcontrolled'] = mapcontrolled
            device = self.devices[uuid].copy()

        return device

    def save_device(self, device, force_save=False):
        uuid = device['deviceid']
        if uuid not in self.devices:
            self.devices[uuid] = DeviceWorker.get_by_id(uuid, device['latitude'], device['longitude'])

        self.devices[uuid] = device.copy()

        if force_save or uuid not in self.devicessavetime or (datetime.utcnow() - self.devicessavetime[uuid]).total_seconds() > 30:
            self.devicessavetime[uuid] = datetime.utcnow()
            if self.devices[uuid].get('last_scanned') is None:
                self.devices[uuid]['last_scanned'] = datetime.utcnow() - timedelta(days=1)

            deviceworkers = {}
            deviceworkers[uuid] = self.devices[uuid].copy()

            if 'route' in deviceworkers[uuid]:
                del deviceworkers[uuid]['route']
            if 'no_overlap' in deviceworkers[uuid]:
                del deviceworkers[uuid]['no_overlap']
            if 'mapcontrolled' in deviceworkers[uuid]:
                del deviceworkers[uuid]['mapcontrolled']

            self.db_update_queue.put((DeviceWorker, deviceworkers))

            if device['fetching'] != 'IDLE':
                scan_location = ScannedLocation.get_by_loc([device['latitude'], device['longitude']])
                ScannedLocation.update_band(scan_location, device['last_updated'])
                if 'teleport' in device['fetching']:
                    scan_location['scanningforts'] = 1
                else:
                    scan_location['scanningforts'] = 0
                self.db_update_queue.put((ScannedLocation, {0: scan_location}))
            if force_save:
                return 'Name saved'

    def gym_img(self):
        team = request.args.get('team')
        level = request.args.get('level')
        raidlevel = request.args.get('raidlevel')
        pkm = request.args.get('pkm')
        pkm_form = request.args.get('form')
        is_in_battle = 'in_battle' in request.args
        is_ex_raid_eligible = 'ex_raid' in request.args
        is_unknown = 'is_unknown' in request.args

        if level is not None:
            level = int(level)
        if raidlevel is not None:
            raidlevel = int(raidlevel)
        if pkm is not None:
            pkm = int(pkm)
        if pkm_form is not None:
            pkm_form = int(pkm_form)

        return send_file(get_gym_icon(team, level, raidlevel, pkm, pkm_form, is_in_battle, is_ex_raid_eligible, is_unknown), mimetype='image/png')

    def get_pokemon_rarity_code(self, pokemonid):
        rarity = self.get_pokemon_rarity(pokemonid)
        rarities = {
            "New Spawn": 0,
            "Common": 1,
            "Uncommon": 2,
            "Rare": 3,
            "Very Rare": 4,
            "Ultra Rare": 5
        }
        return rarities.get(rarity, 0)

    def get_pokemon_rarity(self, pokemonid):
        args = get_args()
        rarity = "New Spawn"
        root_path = args.root_path
        rarities_path = os.path.join(root_path, 'static/dist/data/rarity.json')
        with open(rarities_path) as f:
            data = json.load(f)
            rarity = data.get(str(pokemonid), "New Spawn")

        return rarity

    def feedquest(self):
        self.heartbeat[0] = now()
        args = get_args()
        if args.on_demand_timeout > 0:
            self.control_flags['on_demand'].clear()

        swLat = request.args.get('swLat')
        swLng = request.args.get('swLng')
        neLat = request.args.get('neLat')
        neLng = request.args.get('neLng')

        d = Quest.get_quests(swLat, swLng, neLat, neLng)

        result = ""
        for quest in d:
            if result != "":
                result += "\n"
            result += str(round(quest['latitude'], 5)) + "," + str(round(quest['longitude'], 5)) + ","
            if quest["reward_type"] == "POKEMON_ENCOUNTER":
                result += str(_POKEMONID.values_by_name[quest["reward_item"]].number)
            else:
                result += ""
            result += "," + str(quest['quest_type']) + "," + str(quest["reward_type"])
            if quest["reward_type"] != "STARDUST":
                result += ": " + str(quest["reward_item"])
            if quest["reward_type"] != "POKEMON_ENCOUNTER":
                result += " (" + str(quest["reward_amount"]) + ")"
            now_date = datetime.utcnow()
            ttl = int(round((now_date - quest['last_scanned']).total_seconds() / 60))
            result += ", Scanned " + str(ttl) + "m ago"

        return result.strip()

    def feedpokemon(self):
        self.heartbeat[0] = now()
        args = get_args()
        if args.on_demand_timeout > 0:
            self.control_flags['on_demand'].clear()
        d = {}

        # Request time of this request.
        d['timestamp'] = datetime.utcnow()

        # Request time of previous request.
        if request.args.get('timestamp'):
            timestamp = int(request.args.get('timestamp'))
            timestamp -= 1000  # Overlap, for rounding errors.
        else:
            timestamp = 0

        swLat = request.args.get('swLat')
        swLng = request.args.get('swLng')
        neLat = request.args.get('neLat')
        neLng = request.args.get('neLng')

        oSwLat = request.args.get('oSwLat')
        oSwLng = request.args.get('oSwLng')
        oNeLat = request.args.get('oNeLat')
        oNeLng = request.args.get('oNeLng')

        lastpokemon = request.args.get('lastpokemon')

        weathertypes = {
            0: {
                "name": "None"
            },
            1: {
                "name": "Clear",
                "emoji": u"\u2600",
                "boost": "grass,ground,fire"
            },
            2: {
                "name": "Rainy",
                "emoji": u"\u2614",
                "boost": "water,electric,bug"
            },
            3: {
                "name": "PartlyCloudy",
                "emoji": u"\U0001F324",
                "boost": "normal,rock"
            },
            4: {
                "name": "Overcast",
                "emoji": u"\u2601",
                "boost": "fairy,fighting,poison"
            },
            5: {
                "name": "Windy",
                "emoji": u"\U0001F32C",
                "boost": "dragon,flying,psychic"
            },
            6: {
                "name": "Snow",
                "emoji": u"\u2744",
                "boost": "ice,steel"
            },
            7: {
                "name": "Fog",
                "emoji": u"\U0001F32B",
                "boost": "dark,ghost"
            }
        }

        if request.args.get('pokemon', 'true') == 'true':
            d['lastpokemon'] = request.args.get('pokemon', 'true')

        # If old coords are not equal to current coords we have moved/zoomed!
        if (oSwLng < swLng and oSwLat < swLat and
                oNeLat > neLat and oNeLng > neLng):
            newArea = False  # We zoomed in no new area uncovered.
        elif not (oSwLat == swLat and oSwLng == swLng and
                  oNeLat == neLat and oNeLng == neLng):
            newArea = True
        else:
            newArea = False

        # Pass current coords as old coords.
        d['oSwLat'] = swLat
        d['oSwLng'] = swLng
        d['oNeLat'] = neLat
        d['oNeLng'] = neLng

        if (request.args.get('pokemon', 'true') == 'true' and
                not args.no_pokemon):

            # Exclude ids of Pokemon that are hidden.
            eids = []
            request_eids = request.args.get('eids')
            if request_eids:
                eids = {int(i) for i in request_eids.split(',')}

            if request.args.get('ids'):
                request_ids = request.args.get('ids').split(',')
                ids = [int(x) for x in request_ids if int(x) not in eids]
                d['pokemons'] = convert_pokemon_list(
                    Pokemon.get_active_by_id(ids, swLat, swLng, neLat, neLng))
            elif lastpokemon != 'true':
                # If this is first request since switch on, load
                # all pokemon on screen.
                d['pokemons'] = convert_pokemon_list(
                    Pokemon.get_active(
                        swLat, swLng, neLat, neLng, exclude=eids))
            else:
                # If map is already populated only request modified Pokemon
                # since last request time.
                d['pokemons'] = convert_pokemon_list(
                    Pokemon.get_active(
                        swLat, swLng, neLat, neLng,
                        timestamp=timestamp, exclude=eids))
                if newArea:
                    # If screen is moved add newly uncovered Pokemon to the
                    # ones that were modified since last request time.
                    d['pokemons'] = d['pokemons'] + (
                        convert_pokemon_list(
                            Pokemon.get_active(
                                swLat,
                                swLng,
                                neLat,
                                neLng,
                                exclude=eids,
                                oSwLat=oSwLat,
                                oSwLng=oSwLng,
                                oNeLat=oNeLat,
                                oNeLng=oNeLng)))

            if request.args.get('reids'):
                reids = [int(x) for x in request.args.get('reids').split(',')]
                d['pokemons'] = d['pokemons'] + (
                    convert_pokemon_list(
                        Pokemon.get_active_by_id(reids, swLat, swLng, neLat,
                                                 neLng)))
                d['reids'] = reids

        if request.args.get('seen', 'false') == 'true':
            d['seen'] = Pokemon.get_seen(int(request.args.get('duration')))

        if request.args.get('appearances', 'false') == 'true':
            d['appearances'] = Pokemon.get_appearances(
                request.args.get('pokemonid'),
                int(request.args.get('duration')))

        if request.args.get('appearancesDetails', 'false') == 'true':
            d['appearancesTimes'] = (
                Pokemon.get_appearances_times_by_spawnpoint(
                    request.args.get('pokemonid'),
                    request.args.get('spawnpoint_id'),
                    int(request.args.get('duration'))))

        result = ""
        for pokemon in d['pokemons']:
            if result != "":
                result += "\n"
            result += str(round(pokemon['latitude'], 5)) + "," + str(round(pokemon['longitude'], 5)) + "," + str(pokemon['pokemon_id']) + "," + pokemon['pokemon_name']
            if pokemon['weather_boosted_condition'] > 0 and weathertypes[pokemon['weather_boosted_condition']]:
                result += ", " + weathertypes[pokemon['weather_boosted_condition']]["emoji"] + " " + weathertypes[pokemon['weather_boosted_condition']]["name"]
            rarity = self.get_pokemon_rarity(pokemon['pokemon_id'])
            result += ", " + rarity
            now_date = datetime.utcnow()
            ttl = int(round((pokemon['disappear_time'] - now_date).total_seconds() / 60))
            result += ", " + str(ttl) + "m"

        return result.strip()

    def auth_callback(self):
        session.permanent = True
        code = request.args.get('code')
        if not code:
            log.error('User authentication code not found in callback.')
            abort(403)

        response = self.discord_api.exchange_code(code)
        if not response:
            log.error('Failed OAuth request for user authentication.')
            abort(403)

        valid = self.discord_api.validate_auth(session, response)
        if not valid['auth'] and not valid['url']:
            abort(403)
        elif not valid['auth']:
            return make_response(redirect(valid['url']))

        return make_response(redirect('/'))

    def auth_logout(self):
        session.clear()
        return make_response(redirect('/'))

    def render_robots_txt(self):
        return render_template('robots.txt')

    def render_service_worker_js(self):
        return send_from_directory('static/dist/js', 'serviceWorker.min.js')

    def trusted_device(self, uuid):
        canusedevice = True
        devicename = ""

        args = get_args()

        if self.deviceschecked is None or (datetime.utcnow() - self.deviceschecked).total_seconds() > 300:
            self.trusteddevices = {}
            if args.devices_file:
                with open(args.devices_file) as f:
                    for line in f:
                        line = line.strip()
                        if len(line) == 0:  # Empty line.
                            continue
                        else:  # Coordinate line.
                            deviceid, name = line.split(":")
                            self.trusteddevices[deviceid.strip()] = name.strip()

            self.deviceschecked = datetime.utcnow()

        if len(self.trusteddevices) > 0:
            if uuid in self.trusteddevices:
                devicename = self.trusteddevices[uuid]
            else:
                canusedevice = False

        return canusedevice, devicename

    def webhook(self):
        if request.method == "GET":
            request_json = request.args
        else:
            request_json = request.get_json()
        protos = request_json.get('protos')
        trainerlvl = request_json.get('trainerlvl', 30)

        uuid = request_json.get('uuid')
        if uuid == "":
            return ""

        canusedevice, devicename = self.trusted_device(uuid)
        if not canusedevice:
            return ""

        args = get_args()

        if protos:
            lat = float(request_json.get('latitude', request_json.get('latitude:', 0)))
            lng = float(request_json.get('longitude', request_json.get('longitude:', 0)))

            # Geofence results.
            if not self.geofences:
                from .geofence import Geofences
                self.geofences = Geofences()
            if not args.data_outside_geofences and self.geofences.is_enabled():
                results = self.geofences.get_geofenced_coordinates([(lat, lng, 0)])
                if not results:
                    log.info('The post from %s is coming from outside your geofences. Aborting post.' % uuid)
                    return ""

            if not args.dont_move_map:
                self.location_queue.put((lat, lng, 0))
                self.set_current_location((lat, lng, 0))
                log.info('Changing next location: %s,%s', lat, lng)

            deviceworker = self.get_device(uuid, lat, lng)

            deviceworker['scans'] = deviceworker['scans'] + 1
            deviceworker['last_scanned'] = datetime.utcnow()

            if deviceworker['scanning'] == 0:
                deviceworker['scanning'] = 1
            if deviceworker['fetching'] == 'IDLE':
                deviceworker['latitude'] = lat
                deviceworker['longitude'] = lng

            # Update PoGo version
            pogoversion = re.sub(r'pokemongo/([^\ ]*).*', r'\1', request.headers.get('User-Agent', 'Unknown'))
            if pogoversion != "" and pogoversion != deviceworker['pogoversion']:
                log.info('Device {} updating pogoversion: {} => {}'.format(uuid, deviceworker['pogoversion'], pogoversion))
                deviceworker['pogoversion'] = pogoversion
                self.save_device(deviceworker, True)
            else:
                self.save_device(deviceworker)

            self.track_event('worker', 'sent', uuid)

            return self.parse_map_protos(protos, trainerlvl, deviceworker)
        else:
            return 'wrong'

    def track_event(self, category, action, uuid='555', label=None):
        args = get_args()

        if (args.google_analytics_key == '' or args.google_analytics_key == None):
            return

        last_message_sent = self.ga_alerts.get(uuid, {}).get(category, {}).get(action, {}).get('sent', None)
        if last_message_sent is None or (datetime.utcnow() - last_message_sent).total_seconds() > 30:
            if uuid not in self.ga_alerts:
                self.ga_alerts[uuid] = {}
            if category not in self.ga_alerts[uuid]:
                self.ga_alerts[uuid][category] = {}
            if action not in self.ga_alerts[uuid][category]:
                self.ga_alerts[uuid][category][action] = {
                    'sent': None,
                    'count': 0,
                }
            self.ga_alerts[uuid][category][action]['sent'] = datetime.utcnow()
            value = self.ga_alerts[uuid][category][action]['count']
            data = {
                'v': '1',  # API Version.
                'tid': args.google_analytics_key,  # Tracking ID / Property ID.
                # Anonymous Client Identifier. Ideally, this should be a UUID that
                # is associated with particular user, device, or browser instance.
                'cid': uuid,
                't': 'event',  # Event hit type.
                'ec': category,  # Event category.
                'ea': action,  # Event action.
                'el': label,  # Event label.
                'ev': value,  # Event value, must be an integer
            }

            import requests

            response = requests.post(
                'https://www.google-analytics.com/collect', data=data)

            self.ga_alerts[uuid][category][action]['count'] = 0

            # If the request fails, this will raise a RequestException. Depending
            # on your application's needs, this may be a non-error and can be caught
            # by the caller.
            response.raise_for_status()
        else:
            self.ga_alerts[uuid][category][action]['count'] += 1

    def parse_map_protos(self, protos_dict, trainerlvl, deviceworker):
        pokemon = {}
        nearby_pokemons = {}
        pokestops = {}
        gyms = {}
        gym_details = {}
        pokestop_details = {}
        raids = {}
        quest_result = {}
        pokemon_skipped = 0
        nearby_skipped = 0
        spawn_points = {}
        scan_spawn_points = {}
        sightings = {}
        new_spawn_points = []
        sp_id_list = []
        weather = {}
        gym_members = {}
        gym_pokemon = {}
        gym_encountered = {}
        time_of_day = "NONE"

        now_date = datetime.utcnow()

        args = get_args()

        now_secs = date_secs(now_date)

        scan_location = ScannedLocation.get_by_loc([deviceworker['latitude'], deviceworker['longitude']])

        if 'teleport' in deviceworker['fetching']:
            scan_location['scanningforts'] = 1
        elif deviceworker['fetching'] == 'IDLE':
            scan_location['scanningforts'] = -1
        else:
            scan_location['scanningforts'] = 0

        ScannedLocation.update_band(scan_location, now_date)

        uuid = deviceworker['deviceid']

        if uuid not in self.devices_last_scanned_times:
            self.devices_last_scanned_times[uuid] = {
                'pokemon': None,
                'nearby_pokemon': None,
                'wild_pokemon': None,
                'forts': None,
                'gyms': None,
                'pokestops': None,
            }
        last_scanned_times = self.devices_last_scanned_times[uuid]

        for proto in protos_dict:
            if "GetMapObjects" in proto:
                gmo_response_string = b64decode(proto['GetMapObjects'])
                gmo = GetMapObjectsResponse()
                try:
                    gmo.ParseFromString(gmo_response_string)
                    gmo_response_json = json.loads(MessageToJson(gmo))
                except:
                    continue

                if "mapCells" in gmo_response_json:
                    monmaxdist = 0
                    fortmaxdist = 0
                    for mapcell in gmo_response_json["mapCells"]:
                        if "wildPokemons" in mapcell:
                            last_scanned_times['pokemon'] = now_date
                            last_scanned_times['wild_pokemon'] = now_date

                            encounter_ids = [long(p['encounterId']) for p in mapcell["wildPokemons"]]
                            # For all the wild Pokemon we found check if an active Pokemon is in
                            # the database.
                            with Pokemon.database().execution_context():
                                query = (Pokemon
                                         .select(Pokemon.encounter_id, Pokemon.spawnpoint_id)
                                         .where((Pokemon.disappear_time >= now_date) &
                                                (Pokemon.encounter_id << encounter_ids))
                                         .dicts())

                                # Store all encounter_ids and spawnpoint_ids for the Pokemon in
                                # query.
                                # All of that is needed to make sure it's unique.
                                encountered_pokemon = [
                                    (long(p['encounter_id']), p['spawnpoint_id']) for p in query]

                            for p in mapcell["wildPokemons"]:
                                spawn_id = p['spawnPointId']

                                sp = SpawnPoint.get_by_id(spawn_id, p['latitude'], p['longitude'])
                                sp['last_scanned'] = datetime.utcnow()
                                spawn_points[spawn_id] = sp
                                sp['missed_count'] = 0

                                sighting = {
                                    'encounter_id': long(p['encounterId']),
                                    'spawnpoint_id': spawn_id,
                                    'scan_time': now_date,
                                    'tth_secs': None
                                }

                                # Keep a list of sp_ids to return.
                                sp_id_list.append(spawn_id)

                                # time_till_hidden_ms was overflowing causing a negative integer.
                                # It was also returning a value above 3.6M ms.
                                if 0 < float(p['timeTillHiddenMs']) < 3600000:
                                    d_t_secs = date_secs(datetime.utcfromtimestamp(
                                        now() + float(p['timeTillHiddenMs']) / 1000.0))

                                    # Cover all bases, make sure we're using values < 3600.
                                    # Warning: python uses modulo as the least residue, not as
                                    # remainder, so we don't apply it to the result.
                                    residue_unseen = sp['earliest_unseen'] % 3600
                                    residue_seen = sp['latest_seen'] % 3600

                                    if (residue_seen != residue_unseen or
                                            not sp['last_scanned']):
                                        log.info('TTH found for spawnpoint %s.', sp['id'])
                                        sighting['tth_secs'] = d_t_secs

                                        # Only update when TTH is seen for the first time.
                                        # Just before Pokemon migrations, Niantic sets all TTH
                                        # to the exact time of the migration, not the normal
                                        # despawn time.
                                        sp['latest_seen'] = d_t_secs
                                        sp['earliest_unseen'] = d_t_secs

                                scan_spawn_points[len(scan_spawn_points) + 1] = {
                                    'spawnpoint': sp['id'],
                                    'scannedlocation': scan_location['cellid']}
                                if not sp['last_scanned']:
                                    log.info('New Spawn Point found.')
                                    new_spawn_points.append(sp)

                                if (not SpawnPoint.tth_found(sp) or sighting['tth_secs']):
                                    SpawnpointDetectionData.classify(sp, scan_location, now_secs,
                                                                     sighting)
                                    sightings[long(p['encounterId'])] = sighting

                                sp['last_scanned'] = datetime.utcnow()

                                if ((long(p['encounterId']), spawn_id) in encountered_pokemon) or (long(p['encounterId']) in pokemon):
                                    # If Pokemon has been encountered before don't process it.
                                    pokemon_skipped += 1
                                    continue

                                start_end = SpawnPoint.start_end(sp, 1)
                                seconds_until_despawn = (start_end[1] - now_secs) % 3600
                                disappear_time = now_date + timedelta(seconds=seconds_until_despawn)

                                pokemon_id = _POKEMONID.values_by_name[p['pokemonData']['pokemonId']].number

                                gender = _GENDER.values_by_name[p['pokemonData']["pokemonDisplay"].get('gender', 'GENDER_UNSET')].number
                                costume = _COSTUME.values_by_name[p['pokemonData']["pokemonDisplay"].get('costume', 'COSTUME_UNSET')].number
                                form = _FORM.values_by_name[p['pokemonData']["pokemonDisplay"].get('form', 'FORM_UNSET')].number
                                weather_boosted_condition = _WEATHERCONDITION.values_by_name[p['pokemonData']["pokemonDisplay"].get('weatherBoostedCondition', 'NONE')].number

                                printPokemon(pokemon_id, p['latitude'], p['longitude'],
                                             disappear_time)

                                pokemon[long(p['encounterId'])] = {
                                    'encounter_id': long(p['encounterId']),
                                    'spawnpoint_id': spawn_id,
                                    'pokemon_id': pokemon_id,
                                    'latitude': p['latitude'],
                                    'longitude': p['longitude'],
                                    'disappear_time': disappear_time,
                                    'individual_attack': None,
                                    'individual_defense': None,
                                    'individual_stamina': None,
                                    'move_1': None,
                                    'move_2': None,
                                    'cp': None,
                                    'cp_multiplier': None,
                                    'height': None,
                                    'weight': None,
                                    'gender': gender,
                                    'costume': costume,
                                    'form': form,
                                    'weather_boosted_condition': weather_boosted_condition
                                }

                                distance_m = geopy.distance.vincenty((deviceworker['latitude'], deviceworker['longitude']), (p['latitude'], p['longitude'])).meters
                                if distance_m <= 150:
                                    monmaxdist = max(monmaxdist, distance_m)

                                if 'pokemon' in args.wh_types:
                                    if (pokemon_id in args.webhook_whitelist or
                                        (not args.webhook_whitelist and pokemon_id
                                         not in args.webhook_blacklist)):
                                        wh_poke = pokemon[long(p['encounterId'])].copy()
                                        wh_poke.update({
                                            'disappear_time': calendar.timegm(
                                                disappear_time.timetuple()),
                                            'last_modified_time': now(),
                                            'time_until_hidden_ms': float(p['timeTillHiddenMs']),
                                            'verified': SpawnPoint.tth_found(sp),
                                            'seconds_until_despawn': seconds_until_despawn,
                                            'spawn_start': start_end[0],
                                            'spawn_end': start_end[1],
                                            'player_level': int(trainerlvl),
                                            'individual_attack': 0,
                                            'individual_defense': 0,
                                            'individual_stamina': 0,
                                            'move_1': 0,
                                            'move_2': 0,
                                            'cp': 0,
                                            'cp_multiplier': 0,
                                            'height': 0,
                                            'weight': 0,
                                            'weather_id': weather_boosted_condition,
                                            'expire_timestamp_verified': SpawnPoint.tth_found(sp)
                                        })

                                        rarity = self.get_pokemon_rarity_code(pokemon_id)
                                        wh_poke.update({
                                            'rarity': rarity
                                        })

                                        self.wh_update_queue.put(('pokemon', wh_poke))

                        if "catchablePokemons" in mapcell:
                            last_scanned_times['pokemon'] = now_date
                            last_scanned_times['wild_pokemon'] = now_date

                            encounter_ids = [long(p['encounterId']) for p in mapcell["catchablePokemons"]]
                            # For all the wild Pokemon we found check if an active Pokemon is in
                            # the database.
                            with Pokemon.database().execution_context():
                                query = (Pokemon
                                         .select(Pokemon.encounter_id, Pokemon.spawnpoint_id)
                                         .where((Pokemon.disappear_time >= now_date) &
                                                (Pokemon.encounter_id << encounter_ids))
                                         .dicts())

                                # Store all encounter_ids and spawnpoint_ids for the Pokemon in
                                # query.
                                # All of that is needed to make sure it's unique.
                                encountered_pokemon = [
                                    (long(p['encounter_id']), p['spawnpoint_id']) for p in query]

                            for p in mapcell["catchablePokemons"]:
                                spawn_id = p['spawnPointId']

                                sp = SpawnPoint.get_by_id(spawn_id, p['latitude'], p['longitude'])
                                sp['last_scanned'] = datetime.utcnow()
                                spawn_points[spawn_id] = sp
                                sp['missed_count'] = 0

                                sighting = {
                                    'encounter_id': long(p['encounterId']),
                                    'spawnpoint_id': spawn_id,
                                    'scan_time': now_date,
                                    'tth_secs': None
                                }

                                # Keep a list of sp_ids to return.
                                sp_id_list.append(spawn_id)

                                expirationTimestampMs = float(p.get('expirationTimestampMs', -1))

                                # time_till_hidden_ms was overflowing causing a negative integer.
                                # It was also returning a value above 3.6M ms.
                                if expirationTimestampMs > 0:
                                    d_t_secs = date_secs(datetime.utcfromtimestamp(expirationTimestampMs / 1000.0))

                                    # Cover all bases, make sure we're using values < 3600.
                                    # Warning: python uses modulo as the least residue, not as
                                    # remainder, so we don't apply it to the result.
                                    residue_unseen = sp['earliest_unseen'] % 3600
                                    residue_seen = sp['latest_seen'] % 3600

                                    if (residue_seen != residue_unseen or
                                            not sp['last_scanned']):
                                        log.info('TTH found for spawnpoint %s.', sp['id'])
                                        sighting['tth_secs'] = d_t_secs

                                        # Only update when TTH is seen for the first time.
                                        # Just before Pokemon migrations, Niantic sets all TTH
                                        # to the exact time of the migration, not the normal
                                        # despawn time.
                                        sp['latest_seen'] = d_t_secs
                                        sp['earliest_unseen'] = d_t_secs

                                scan_spawn_points[len(scan_spawn_points) + 1] = {
                                    'spawnpoint': sp['id'],
                                    'scannedlocation': scan_location['cellid']}
                                if not sp['last_scanned']:
                                    log.info('New Spawn Point found.')
                                    new_spawn_points.append(sp)

                                if (not SpawnPoint.tth_found(sp) or sighting['tth_secs']):
                                    SpawnpointDetectionData.classify(sp, scan_location, now_secs,
                                                                     sighting)
                                    sightings[long(p['encounterId'])] = sighting

                                sp['last_scanned'] = datetime.utcnow()

                                if ((long(p['encounterId']), spawn_id) in encountered_pokemon) or (long(p['encounterId']) in pokemon):
                                    # If Pokemon has been encountered before don't process it.
                                    pokemon_skipped += 1
                                    continue

                                start_end = SpawnPoint.start_end(sp, 1)
                                seconds_until_despawn = (start_end[1] - now_secs) % 3600
                                disappear_time = now_date + timedelta(seconds=seconds_until_despawn)

                                pokemon_id = _POKEMONID.values_by_name[p['pokemonId']].number

                                gender = _GENDER.values_by_name[p["pokemonDisplay"].get('gender', 'GENDER_UNSET')].number
                                costume = _COSTUME.values_by_name[p["pokemonDisplay"].get('costume', 'COSTUME_UNSET')].number
                                form = _FORM.values_by_name[p["pokemonDisplay"].get('form', 'FORM_UNSET')].number
                                weather_boosted_condition = _WEATHERCONDITION.values_by_name[p["pokemonDisplay"].get('weatherBoostedCondition', 'NONE')].number

                                printPokemon(pokemon_id, p['latitude'], p['longitude'],
                                             disappear_time)

                                pokemon[long(p['encounterId'])] = {
                                    'encounter_id': long(p['encounterId']),
                                    'spawnpoint_id': spawn_id,
                                    'pokemon_id': pokemon_id,
                                    'latitude': p['latitude'],
                                    'longitude': p['longitude'],
                                    'disappear_time': disappear_time,
                                    'individual_attack': None,
                                    'individual_defense': None,
                                    'individual_stamina': None,
                                    'move_1': None,
                                    'move_2': None,
                                    'cp': None,
                                    'cp_multiplier': None,
                                    'height': None,
                                    'weight': None,
                                    'gender': gender,
                                    'costume': costume,
                                    'form': form,
                                    'weather_boosted_condition': weather_boosted_condition
                                }

                                distance_m = geopy.distance.vincenty((deviceworker['latitude'], deviceworker['longitude']), (p['latitude'], p['longitude'])).meters
                                if distance_m <= 150:
                                    monmaxdist = max(monmaxdist, distance_m)

                                if 'pokemon' in args.wh_types:
                                    if (pokemon_id in args.webhook_whitelist or
                                        (not args.webhook_whitelist and pokemon_id
                                         not in args.webhook_blacklist)):
                                        wh_poke = pokemon[long(p['encounterId'])].copy()
                                        wh_poke.update({
                                            'disappear_time': calendar.timegm(
                                                disappear_time.timetuple()),
                                            'last_modified_time': now(),
                                            'time_until_hidden_ms': expirationTimestampMs,
                                            'verified': SpawnPoint.tth_found(sp),
                                            'seconds_until_despawn': seconds_until_despawn,
                                            'spawn_start': start_end[0],
                                            'spawn_end': start_end[1],
                                            'player_level': int(trainerlvl),
                                            'individual_attack': 0,
                                            'individual_defense': 0,
                                            'individual_stamina': 0,
                                            'move_1': 0,
                                            'move_2': 0,
                                            'cp': 0,
                                            'cp_multiplier': 0,
                                            'height': 0,
                                            'weight': 0,
                                            'weather_id': weather_boosted_condition,
                                            'expire_timestamp_verified': SpawnPoint.tth_found(sp)
                                        })

                                        rarity = self.get_pokemon_rarity_code(pokemon_id)
                                        wh_poke.update({
                                            'rarity': rarity
                                        })

                                        self.wh_update_queue.put(('pokemon', wh_poke))

                        if "nearbyPokemons" in mapcell:
                            last_scanned_times['pokemon'] = now_date
                            last_scanned_times['nearby_pokemon'] = now_date

                            nearby_encounter_ids = [long(p['encounterId']) for p in mapcell["nearbyPokemons"]]
                            # For all the wild Pokemon we found check if an active Pokemon is in
                            # the database.
                            with PokestopMember.database().execution_context():
                                query = (PokestopMember
                                         .select(PokestopMember.encounter_id, PokestopMember.pokestop_id)
                                         .where((PokestopMember.disappear_time >= now_date) &
                                                (PokestopMember.encounter_id << nearby_encounter_ids))
                                         .dicts())

                                # Store all encounter_ids and spawnpoint_ids for the Pokemon in
                                # query.
                                # All of that is needed to make sure it's unique.
                                nearby_encountered_pokemon = [
                                    (long(p['encounter_id']), p['pokestop_id']) for p in query]

                            for p in mapcell["nearbyPokemons"]:
                                pokestop_id = p.get('fortId')
                                if not pokestop_id:
                                    continue
                                encounter_id = p.get('encounterId')
                                if not encounter_id:
                                    continue
                                if ((encounter_id, pokestop_id) in nearby_encountered_pokemon) or (encounter_id in nearby_pokemons):
                                    nearby_skipped += 1
                                    continue

                                disappear_time = now_date + timedelta(seconds=600)

                                pokemon_id = _POKEMONID.values_by_name[p['pokemonId']].number

                                distance = round(p.get('distanceInMeters', 0), 5)

                                gender = _GENDER.values_by_name[p["pokemonDisplay"].get('gender', 'GENDER_UNSET')].number
                                costume = _COSTUME.values_by_name[p["pokemonDisplay"].get('costume', 'COSTUME_UNSET')].number
                                form = _FORM.values_by_name[p["pokemonDisplay"].get('form', 'FORM_UNSET')].number
                                weather_boosted_condition = _WEATHERCONDITION.values_by_name[p["pokemonDisplay"].get('weatherBoostedCondition', 'NONE')].number

                                nearby_pokemons[long(encounter_id)] = {
                                    'encounter_id': long(encounter_id),
                                    'pokestop_id': p['fortId'],
                                    'pokemon_id': pokemon_id,
                                    'disappear_time': disappear_time,
                                    'gender': gender,
                                    'costume': costume,
                                    'form': form,
                                    'weather_boosted_condition': weather_boosted_condition,
                                    'distance': distance
                                }
                                if nearby_pokemons[long(encounter_id)]['costume'] < -1:
                                    nearby_pokemons[long(encounter_id)]['costume'] = -1
                                if nearby_pokemons[long(encounter_id)]['form'] < -1:
                                    nearby_pokemons[long(encounter_id)]['form'] = -1

                                pokestopdetails = pokestop_details.get(p['fortId'], Pokestop.get_pokestop_details(p['fortId']))
                                pokestop_url = p.get('fortImageUrl', "").replace('http://', 'https://')
                                if pokestopdetails:
                                    pokestop_name = pokestopdetails.get("name")
                                    pokestop_description = pokestopdetails.get("description")
                                    pokestop_url = pokestop_url if pokestop_url != "" else pokestopdetails["url"]

                                    pokestop_details[p['fortId']] = {
                                        'pokestop_id': p['fortId'],
                                        'name': pokestop_name,
                                        'description': pokestop_description,
                                        'url': pokestop_url
                                    }

                        if "forts" in mapcell:
                            last_scanned_times['forts'] = now_date

                            stop_ids = [f['id'] for f in mapcell["forts"]]
                            if stop_ids:
                                with Pokemon.database().execution_context():
                                    query = (Pokestop.select(
                                        Pokestop.pokestop_id, Pokestop.last_modified).where(
                                            (Pokestop.pokestop_id << stop_ids)).dicts())
                                    encountered_pokestops = [(f['pokestop_id'], int(
                                        (f['last_modified'] - datetime(1970, 1, 1)).total_seconds()))
                                        for f in query]
                            for fort in mapcell["forts"]:
                                if fort.get("type") == "CHECKPOINT":
                                    last_scanned_times['pokestops'] = now_date

                                    activeFortModifier = fort.get('activeFortModifier', [])
                                    if 'ITEM_TROY_DISK' in activeFortModifier:
                                        lure_expiration = datetime.utcfromtimestamp(long(fort['lastModifiedTimestampMs']) / 1000) + timedelta(minutes=args.lure_duration)
                                        lureInfo = fort.get('lureInfo')
                                        if lureInfo is not None:
                                            active_pokemon_id = _POKEMONID.values_by_name[lureInfo.get('activePokemonId', 'MISSINGNO')].number,
                                            active_pokemon_expiration = datetime.utcfromtimestamp(long(lureInfo.get('lureExpiresTimestampMs')) / 1000)
                                        else:
                                            active_pokemon_id = None
                                            active_pokemon_expiration = None
                                    else:
                                        lure_expiration = None
                                        active_pokemon_id = None
                                        active_pokemon_expiration = None

                                    pokestops[fort['id']] = {
                                        'pokestop_id': fort['id'],
                                        'enabled': fort.get('enabled', False),
                                        'latitude': fort['latitude'],
                                        'longitude': fort['longitude'],
                                        'last_modified': datetime.utcfromtimestamp(
                                            float(fort['lastModifiedTimestampMs']) / 1000.0),
                                        'lure_expiration': lure_expiration,
                                        'active_fort_modifier': json.dumps(activeFortModifier),
                                        'active_pokemon_id': active_pokemon_id,
                                        'active_pokemon_expiration': active_pokemon_expiration
                                    }

                                    distance_m = geopy.distance.vincenty((deviceworker['latitude'], deviceworker['longitude']), (fort['latitude'], fort['longitude'])).meters
                                    if distance_m <= 1500:
                                        fortmaxdist = max(fortmaxdist, distance_m)

                                    pokestopdetails = pokestop_details.get(fort['id'], Pokestop.get_pokestop_details(fort['id']))
                                    pokestop_name = str(fort['latitude']) + ',' + str(fort['longitude'])
                                    pokestop_description = ""
                                    pokestop_url = fort.get('imageUrl', "").replace('http://', 'https://')
                                    if pokestopdetails:
                                        pokestop_name = pokestopdetails.get("name", pokestop_name)
                                        pokestop_description = pokestopdetails.get("description", pokestop_description)
                                        pokestop_url = pokestop_url if pokestop_url != "" else pokestopdetails["url"]

                                    pokestop_details[fort['id']] = {
                                        'pokestop_id': fort['id'],
                                        'name': pokestop_name,
                                        'description': pokestop_description,
                                        'url': pokestop_url
                                    }

                                    if ((fort['id'], int(float(fort['lastModifiedTimestampMs']) / 1000.0))
                                            in encountered_pokestops):
                                        # If pokestop has been encountered before and hasn't
                                        # changed don't process it.
                                        continue

                                    if 'pokestop' in args.wh_types or (
                                            'lure' in args.wh_types and
                                            lure_expiration is not None):
                                        l_e = None
                                        if lure_expiration is not None:
                                            l_e = calendar.timegm(lure_expiration.timetuple())
                                        wh_pokestop = pokestops[fort['id']].copy()
                                        wh_pokestop.update({
                                            'pokestop_id': fort['id'],
                                            'last_modified': float(fort['lastModifiedTimestampMs']),
                                            'lure_expiration': l_e,
                                        })
                                        self.wh_update_queue.put(('pokestop', wh_pokestop))
                                else:
                                    last_scanned_times['gyms'] = now_date

                                    b64_gym_id = str(fort['id'])
                                    park = Gym.get_gyms_park(fort['id'])

                                    gyms[fort['id']] = {
                                        'gym_id':
                                            fort['id'],
                                        'team_id':
                                            _TEAMCOLOR.values_by_name[fort.get('ownedByTeam', 'NEUTRAL')].number,
                                        'park':
                                            park,
                                        'guard_pokemon_id':
                                            _POKEMONID.values_by_name[fort.get('guardPokemonId', 'MISSINGNO')].number,
                                        'slots_available':
                                            fort["gymDisplay"].get('slotsAvailable', 0),
                                        'total_cp':
                                            fort["gymDisplay"].get('totalGymCp', 0),
                                        'enabled':
                                            fort.get('enabled', False),
                                        'latitude':
                                            fort['latitude'],
                                        'longitude':
                                            fort['longitude'],
                                        'last_modified':
                                            datetime.utcfromtimestamp(
                                                float(fort['lastModifiedTimestampMs']) / 1000.0),
                                        'is_in_battle':
                                            fort.get('isInBattle', False),
                                        'is_ex_raid_eligible':
                                            fort.get('isExRaidEligible', False)
                                    }

                                    distance_m = geopy.distance.vincenty((deviceworker['latitude'], deviceworker['longitude']), (fort['latitude'], fort['longitude'])).meters
                                    if distance_m <= 1500:
                                        fortmaxdist = max(fortmaxdist, distance_m)

                                    gym_id = fort['id']

                                    gymdetails = gym_details.get(gym_id, Gym.get_gym_details(gym_id))
                                    gym_name = str(fort['latitude']) + ',' + str(fort['longitude'])
                                    gym_description = ""
                                    gym_url = fort.get('imageUrl', "").replace('http://', 'https://')
                                    if gymdetails:
                                        gym_name = gymdetails.get("name", gym_name)
                                        gym_description = gymdetails.get("description", gym_description)
                                        gym_url = gym_url if gym_url != "" else gymdetails["url"]

                                    gym_details[gym_id] = {
                                        'gym_id': gym_id,
                                        'name': gym_name,
                                        'description': gym_description,
                                        'url': gym_url
                                    }

                                    if 'gym' in args.wh_types:
                                        raid_active_until = 0
                                        if 'raidInfo' in fort and not fort["raidInfo"].get('complete', False):
                                            raid_battle_ms = float(fort['raidInfo']['raidBattleMs'])
                                            raid_end_ms = float(fort['raidInfo']['raidEndMs'])

                                            if raid_battle_ms / 1000 > time.time():
                                                raid_active_until = raid_end_ms / 1000

                                        # Explicitly set 'webhook_data', in case we want to change
                                        # the information pushed to webhooks.  Similar to above
                                        # and previous commits.
                                        wh_gym = gyms[fort['id']].copy()

                                        wh_gym.update({
                                            'gym_id':
                                                b64_gym_id,
                                            'gym_name':
                                                gym_name,
                                            'lowest_pokemon_motivation':
                                                float(fort["gymDisplay"].get('lowestPokemonMotivation', 0)),
                                            'occupied_since':
                                                float(fort["gymDisplay"].get('occupiedMillis', 0)),
                                            'last_modified':
                                                float(fort['lastModifiedTimestampMs']),
                                            'raid_active_until':
                                                raid_active_until,
                                            'ex_raid_eligible':
                                                fort.get('isExRaidEligible', False)
                                        })

                                        self.wh_update_queue.put(('gym', wh_gym))

                                    if 'gym-info' in args.wh_types:
                                        webhook_data = {
                                            'id': str(gym_id),
                                            'latitude': fort['latitude'],
                                            'longitude': fort['longitude'],
                                            'team': _TEAMCOLOR.values_by_name[fort.get('ownedByTeam', 'NEUTRAL')].number,
                                            'name': gym_name,
                                            'description': gym_description,
                                            'url': gym_url,
                                            'pokemon': [],
                                        }

                                        self.wh_update_queue.put(('gym_details', webhook_data))

                                    if 'raidInfo' in fort and not fort["raidInfo"].get('complete', False) and not fort["raidInfo"].get('isExclusive', False):
                                        raidinfo = fort["raidInfo"]
                                        raidpokemonid = raidinfo['raidPokemon']['pokemonId'] if 'raidPokemon' in raidinfo and 'pokemonId' in raidinfo['raidPokemon'] else None
                                        if raidpokemonid:
                                            raidpokemonid = _POKEMONID.values_by_name[raidpokemonid].number
                                        raidpokemoncp = raidinfo['raidPokemon']['cp'] if 'raidPokemon' in raidinfo and 'cp' in raidinfo['raidPokemon'] else None
                                        raidpokemonmove1 = raidinfo['raidPokemon']['move1'] if 'raidPokemon' in raidinfo and 'move1' in raidinfo['raidPokemon'] else None
                                        if raidpokemonmove1:
                                            raidpokemonmove1 = _POKEMONMOVE.values_by_name[raidpokemonmove1].number
                                        raidpokemonmove2 = raidinfo['raidPokemon']['move2'] if 'raidPokemon' in raidinfo and 'move2' in raidinfo['raidPokemon'] else None
                                        if raidpokemonmove2:
                                            raidpokemonmove2 = _POKEMONMOVE.values_by_name[raidpokemonmove2].number
                                        raidpokemonform = _FORM.values_by_name[raidinfo.get('raidPokemon', {}).get("pokemonDisplay", {}).get('form', 'FORM_UNSET')].number

                                        raids[fort['id']] = {
                                            'gym_id': fort['id'],
                                            'level': _RAIDLEVEL.values_by_name[raidinfo['raidLevel']].number,
                                            'spawn': datetime.utcfromtimestamp(
                                                float(raidinfo['raidSpawnMs']) / 1000.0),
                                            'start': datetime.utcfromtimestamp(
                                                float(raidinfo['raidBattleMs']) / 1000.0),
                                            'end': datetime.utcfromtimestamp(
                                                float(raidinfo['raidEndMs']) / 1000.0),
                                            'pokemon_id': raidpokemonid,
                                            'cp': raidpokemoncp,
                                            'move_1': raidpokemonmove1,
                                            'move_2': raidpokemonmove2,
                                            'form': raidpokemonform
                                        }

                                        if ('egg' in args.wh_types and
                                                ('raidPokemon' not in raidinfo or 'pokemonId' not in raidinfo['raidPokemon'])) or (
                                                    'raid' in args.wh_types and
                                                    'raidPokemon' in raidinfo and 'pokemonId' in raidinfo['raidPokemon']):
                                            wh_raid = raids[fort['id']].copy()
                                            wh_raid.update({
                                                'gym_id': b64_gym_id,
                                                'team_id': _TEAMCOLOR.values_by_name[fort.get('ownedByTeam', 'NEUTRAL')].number,
                                                'spawn': float(raidinfo['raidSpawnMs']) / 1000,
                                                'start': round(float(raidinfo['raidBattleMs']) / 1000),
                                                'end': round(float(raidinfo['raidEndMs']) / 1000),
                                                'latitude': fort['latitude'],
                                                'longitude': fort['longitude'],
                                                'cp': raidpokemoncp,
                                                'move_1': raidpokemonmove1 if raidpokemonmove1 else 0,
                                                'move_2': raidpokemonmove2 if raidpokemonmove2 else 0,
                                                'is_ex_raid_eligible':
                                                    fort.get('isExRaidEligible', False),
                                                'is_ex_eligible':
                                                    fort.get('isExRaidEligible', False),
                                                'ex_raid_eligible':
                                                    fort.get('isExRaidEligible', False),
                                                'name': gym_name,
                                                'description': gym_description,
                                                'url': gym_url,

                                            })
                                            self.wh_update_queue.put(('raid', wh_raid))
                    if monmaxdist > 0:
                        scan_location['monradius'] = round(monmaxdist)
                    if fortmaxdist > 0:
                        scan_location['fortradius'] = round(fortmaxdist)
                if "timeOfDay" in gmo_response_json:
                    time_of_day = gmo_response_json.get("timeOfDay", "NONE")
                if "clientWeather" in gmo_response_json:
                    clientWeather = gmo_response_json["clientWeather"]
                    for cw in clientWeather:
                        # Parse Map Weather Information
                        s2_cell_id = cw.get("s2CellId")
                        if not s2_cell_id:
                            continue

                        display_weather = cw.get("displayWeather")
                        gameplay_weather = cw.get("gameplayWeather")
                        weather_alerts = cw.get("alerts")

                        s2s_cell_id = s2sphere.CellId(long(s2_cell_id))
                        s2s_cell = s2sphere.Cell(s2s_cell_id)
                        s2s_center = s2sphere.LatLng.from_point(s2s_cell.get_center())
                        s2s_lat = s2s_center.lat().degrees
                        s2s_lng = s2s_center.lng().degrees

                        weather[s2_cell_id] = {
                            's2_cell_id': s2_cell_id,
                            'latitude': s2s_lat,
                            'longitude': s2s_lng,
                            'time_of_day': _GETMAPOBJECTSRESPONSE_TIMEOFDAY.values_by_name[time_of_day].number,
                        }

                        if display_weather:
                            weather[s2_cell_id].update({
                                'cloud_level': _DISPLAYWEATHER_DISPLAYLEVEL.values_by_name[display_weather.get("cloudLevel", "LEVEL_0")].number,
                                'rain_level': _DISPLAYWEATHER_DISPLAYLEVEL.values_by_name[display_weather.get("rainLevel", "LEVEL_0")].number,
                                'wind_level': _DISPLAYWEATHER_DISPLAYLEVEL.values_by_name[display_weather.get("windLevel", "LEVEL_0")].number,
                                'snow_level': _DISPLAYWEATHER_DISPLAYLEVEL.values_by_name[display_weather.get("snowLevel", "LEVEL_0")].number,
                                'fog_level': _DISPLAYWEATHER_DISPLAYLEVEL.values_by_name[display_weather.get("fogLevel", "LEVEL_0")].number,
                                'wind_direction': display_weather.get("windDirection", 0),
                            })

                        if gameplay_weather:
                            gameplay_weathercondition = gameplay_weather.get("gameplayCondition", "NONE")
                            weather[s2_cell_id].update({
                                'gameplay_weather': _GAMEPLAYWEATHER_WEATHERCONDITION.values_by_name[gameplay_weathercondition].number,
                            })

                        if weather_alerts:
                            severity = "NONE"
                            warn_weather = False

                            for wa in weather_alerts:
                                severity = wa.get("severity", "NONE")
                                warn_weather = wa.get("warnWeather", False)

                            weather[s2_cell_id].update({
                                'severity': _WEATHERALERT_SEVERITY.values_by_name[severity].number,
                                'warn_weather': warn_weather,
                            })

        for proto in protos_dict:
            if "GetPlayerResponse" in proto:
                get_player_response_string = b64decode(proto["GetPlayerResponse"])

                gpr = GetPlayerResponse()

                try:
                    gpr.ParseFromString(get_player_response_string)
                    get_player_response_json = json.loads(MessageToJson(gpr))
                except:
                    continue

                playernickname = get_player_response_json.get("playerData", {}).get("username", "")
                if playernickname != "" and playernickname != deviceworker["name"] and args.use_username and len(playernickname) < 16 and 'quest_' not in playernickname:
                    deviceworker["name"] = playernickname
                    self.save_device(deviceworker, True)

            if "GymGetInfoResponse" in proto:
                gym_get_info_response_string = b64decode(proto["GymGetInfoResponse"])

                ggir = GymGetInfoResponse()

                try:
                    ggir.ParseFromString(gym_get_info_response_string)
                    gym_get_info_response_json = json.loads(MessageToJson(ggir))
                except:
                    continue

                gymstatusdefenders = gym_get_info_response_json.get("gymStatusAndDefenders")
                if not gymstatusdefenders:
                    continue

                fort = gymstatusdefenders.get("pokemonFortProto")
                if not fort:
                    continue

                gym_id = fort["id"]

                gymdefenders = gymstatusdefenders.get('gymDefender', [])

                park = Gym.get_gyms_park(gym_id)

                gyms[fort['id']] = {
                    'gym_id':
                        fort['id'],
                    'team_id':
                        _TEAMCOLOR.values_by_name[fort.get('ownedByTeam', 'NEUTRAL')].number,
                    'park':
                        park,
                    'guard_pokemon_id':
                        _POKEMONID.values_by_name[fort.get('guardPokemonId', 'MISSINGNO')].number,
                    'slots_available':
                        6 - len(gymdefenders),
                    'total_cp':
                        float(fort["gymDisplay"].get('totalGymCp', 0)),
                    'enabled':
                        fort.get('enabled', False),
                    'latitude':
                        fort['latitude'],
                    'longitude':
                        fort['longitude'],
                    'last_modified':
                        datetime.utcfromtimestamp(
                            float(fort['lastModifiedTimestampMs']) / 1000.0),
                    'is_in_battle':
                        fort.get('isInBattle', False),
                    'is_ex_raid_eligible':
                        fort.get('isExRaidEligible', False)
                }

                gym_encountered[gym_id] = gyms[gym_id].copy()

                gymdetails = gym_details.get(gym_id, Gym.get_gym_details(gym_id))
                gym_name = gym_get_info_response_json["name"]
                gym_description = gym_get_info_response_json.get("description", "")
                gym_url = gym_get_info_response_json.get("url", "").replace('http://', 'https://')

                gym_details[gym_id] = {
                    'gym_id': gym_id,
                    'name': gym_name,
                    'description': gym_description,
                    'url': gym_url
                }

                if 'gym' in args.wh_types:
                    wh_gym = gyms[fort['id']].copy()

                    wh_gym.update({
                        'gym_id':
                            gym_id,
                        'gym_name':
                            gym_name,
                        'lowest_pokemon_motivation':
                            float(fort["gymDisplay"].get('lowestPokemonMotivation', 0)),
                        'occupied_since':
                            float(fort["gymDisplay"].get('occupiedMillis', 0)),
                        'last_modified':
                            float(fort['lastModifiedTimestampMs']),
                        'ex_raid_eligible':
                            fort.get('isExRaidEligible', False),
                        'is_ex_eligible':
                            fort.get('isExRaidEligible', False)
                    })

                    self.wh_update_queue.put(('gym', wh_gym))

                webhook_data = {
                    'id': str(gym_id),
                    'latitude': fort['latitude'],
                    'longitude': fort['longitude'],
                    'team': _TEAMCOLOR.values_by_name[fort.get('ownedByTeam', 'NEUTRAL')].number,
                    'name': gym_name,
                    'description': gym_description,
                    'url': gym_url,
                    'pokemon': [],
                }

                i = 0
                for member in gymdefenders:
                    motivatedpokemon = member.get("motivatedPokemon")
                    if not motivatedpokemon:
                        continue
                    gympokemon = motivatedpokemon.get("pokemon")
                    gym_members[i] = {
                        'gym_id':
                            gym_id,
                        'pokemon_uid':
                            gympokemon.get("id"),
                        'cp_decayed':
                            int(motivatedpokemon.get('cpNow', 0)),
                        'deployment_time':
                            datetime.utcnow() -
                            timedelta(milliseconds=float(member.get("deploymentTotals", {}).get("deploymentDurationMs", 0)))
                    }
                    gym_pokemon[i] = {
                        'pokemon_uid': gympokemon.get("id"),
                        'pokemon_id': _POKEMONID.values_by_name[gympokemon.get("pokemonId", 'MISSINGNO')].number,
                        'cp': int(motivatedpokemon.get("cpWhenDeployed", 0)),
                        'num_upgrades': int(gympokemon.get("numUpgrades", 0)),
                        'move_1': _POKEMONMOVE.values_by_name[gympokemon.get("move1")].number,
                        'move_2': _POKEMONMOVE.values_by_name[gympokemon.get("move2")].number,
                        'height': float(gympokemon.get("heightM", 0)),
                        'weight': float(gympokemon.get("weightKg", 0)),
                        'stamina': int(gympokemon.get("stamina", 0)),
                        'stamina_max': int(gympokemon.get("staminaMax", 0)),
                        'cp_multiplier': float(gympokemon.get("cpMultiplier", 0)),
                        'additional_cp_multiplier': float(gympokemon.get("additionalCpMultiplier", 0)),
                        'iv_defense': int(gympokemon.get("individualDefense", 0)),
                        'iv_stamina': int(gympokemon.get("individualStamina", 0)),
                        'iv_attack': int(gympokemon.get("individualAttack", 0)),
                        'costume': _COSTUME.values_by_name[gympokemon.get("pokemonDisplay", {}).get("costume", 'COSTUME_UNSET')].number,
                        'form': _FORM.values_by_name[gympokemon.get("pokemonDisplay", {}).get("form", 'FORM_UNSET')].number,
                        'shiny': gympokemon.get("pokemonDisplay", {}).get("shiny"),
                        'last_seen': datetime.utcnow(),
                    }

                    if 'gym-info' in args.wh_types:
                        wh_pokemon = gym_pokemon[i].copy()
                        del wh_pokemon['last_seen']
                        wh_pokemon.update({
                            'cp_decayed':
                                int(motivatedpokemon.get('cpNow', 0)),
                            'deployment_time': calendar.timegm(
                                gym_members[i]['deployment_time'].timetuple())
                        })
                        webhook_data['pokemon'].append(wh_pokemon)

                    i += 1

                if 'gym-info' in args.wh_types:
                    self.wh_update_queue.put(('gym_details', webhook_data))

            if "FortDetailsResponse" in proto:
                fort_details_response_string = b64decode(proto["FortDetailsResponse"])

                fdr = FortDetailsResponse()

                try:
                    fdr.ParseFromString(fort_details_response_string)
                    fort_details_response_json = json.loads(MessageToJson(fdr))
                except:
                    continue

                fort_id = fort_details_response_json.get("fortId")
                if not fort_id:
                    continue

                fort_type = fort_details_response_json.get("type", "")

                if fort_type == "CHECKPOINT":
                    fort_name = fort_details_response_json.get("name", "")
                    fort_description = fort_details_response_json.get("description", "")
                    fort_imageurls = fort_details_response_json.get("imageUrls", [])
                    fort_imageurl = fort_imageurls[0].replace('http://', 'https://') if len(fort_imageurls) else ""

                    pokestop_details[fort_id] = {
                        'pokestop_id': fort_id,
                        'name': fort_name,
                        'description': fort_description,
                        'url': fort_imageurl
                    }

            if "FortSearchResponse" in proto:
                fort_search_response_string = b64decode(proto['FortSearchResponse'])

                frs = FortSearchResponse()

                try:
                    frs.ParseFromString(fort_search_response_string)
                    fort_search_response_json = json.loads(MessageToJson(frs))
                except:
                    continue

                if fort_search_response_json['result'] == 'INVENTORY_FULL' and 'devices' in args.wh_types:
                    wh_worker = {
                        'uuid': deviceworker['deviceid'],
                        'name': deviceworker['name'],
                        'type': 'inventory_full',
                        'message': 'The device has no room for new items, clear your bag.'
                    }
                    self.wh_update_queue.put(('devices', wh_worker))

                elif 'challengeQuest' in fort_search_response_json:
                    utcnow_datetime = datetime.utcnow()

                    quest_json = fort_search_response_json["challengeQuest"]["quest"]

                    quest_pokestop = pokestops.get(quest_json["fortId"], Pokestop.get_stop(quest_json["fortId"]))

                    pokestop_timezone_offset = get_timezone_offset(quest_pokestop['latitude'], quest_pokestop['longitude'])

                    pokestop_localtime = utcnow_datetime + timedelta(minutes=pokestop_timezone_offset)
                    next_day_localtime = datetime(
                        year=pokestop_localtime.year,
                        month=pokestop_localtime.month,
                        day=pokestop_localtime.day
                    ) + timedelta(days=1)
                    next_day_utc = next_day_localtime - timedelta(minutes=pokestop_timezone_offset)

                    quest_result[quest_json['fortId']] = {
                        'pokestop_id': quest_json['fortId'],
                        'quest_type': quest_json['questType'],
                        'goal': quest_json['goal']['target'],
                        'reward_type': quest_json['questRewards'][0]['type'],
                        'reward_item': None,
                        'reward_amount': None,
                        'quest_json': json.dumps(quest_json),
                        'last_scanned': datetime.utcnow(),
                        'expiration': next_day_utc
                    }
                    if quest_json["questRewards"][0]["type"] == "STARDUST":
                        quest_result[quest_json["fortId"]]["reward_amount"] = quest_json["questRewards"][0]["stardust"]
                    elif quest_json["questRewards"][0]["type"] == "POKEMON_ENCOUNTER":
                        quest_result[quest_json["fortId"]]["reward_item"] = quest_json["questRewards"][0]["pokemonEncounter"]["pokemonId"]
                    elif quest_json["questRewards"][0]["type"] == "ITEM":
                        quest_result[quest_json["fortId"]]["reward_amount"] = quest_json["questRewards"][0]["item"]["amount"]
                        quest_result[quest_json["fortId"]]["reward_item"] = quest_json["questRewards"][0]["item"]["item"]

                    if 'devices' in args.wh_types:
                        for reward in quest_json["questRewards"]:
                            rewardtype = _QUESTREWARD_TYPE.values_by_name[reward["type"]].number
                            if rewardtype == 7 and reward["pokemonEncounter"].get("pokemonDisplay", {}).get("shiny", False):
                                wh_worker = {
                                    'uuid': deviceworker['deviceid'],
                                    'name': deviceworker['name'],
                                    'type': 'shiny_quest',
                                    'message': 'A Shiny Quest was discovered for this device at ' + str(quest_json["fortId"]) + '.'
                                }
                                self.wh_update_queue.put(('devices', wh_worker))

                    if 'quest' in args.wh_types:
                        try:
                            wh_quest = quest_result[quest_json["fortId"]].copy()
                            if quest_pokestop:
                                pokestopdetails = pokestop_details.get(quest_json["fortId"], Pokestop.get_pokestop_details(quest_json["fortId"]))

                                wh_quest.update(
                                    {
                                        "latitude": quest_pokestop["latitude"],
                                        "longitude": quest_pokestop["longitude"],
                                        "last_scanned": calendar.timegm(datetime.utcnow().timetuple()),
                                        "expiration": calendar.timegm(next_day_utc.timetuple()),
                                    }
                                )

                                wh_quest.update(
                                    {
                                        "type": _QUESTTYPE.values_by_name[quest_json['questType']].number,
                                        "target": quest_json['goal']['target'],
                                        "pokestop_name": pokestopdetails["name"],
                                        "pokestop_url": pokestopdetails["url"],
                                        "updated": calendar.timegm(datetime.utcnow().timetuple()),
                                    }
                                )

                                rewards = []
                                conditions = []

                                for reward in quest_json["questRewards"]:
                                    rewardtype = _QUESTREWARD_TYPE.values_by_name[reward["type"]].number
                                    info = {}
                                    if rewardtype == 2:
                                        info = {
                                            "item_id": _ITEMID.values_by_name[reward["item"]["item"]].number,
                                            "amount": reward["item"]["amount"],
                                        }
                                    elif rewardtype == 3:
                                        info = {
                                            "amount": reward["stardust"],
                                        }
                                    elif rewardtype == 7:
                                        info = {
                                            "pokemon_id": _POKEMONID.values_by_name[reward["pokemonEncounter"]["pokemonId"]].number,
                                            "costume_id": _COSTUME.values_by_name[reward["pokemonEncounter"].get("pokemonDisplay", {}).get("costume", 'COSTUME_UNSET')].number,
                                            "form_id": _FORM.values_by_name[reward["pokemonEncounter"].get("pokemonDisplay", {}).get("form", 'FORM_UNSET')].number,
                                            "gender_id": _GENDER.values_by_name[reward["pokemonEncounter"].get("pokemonDisplay", {}).get('gender', 'GENDER_UNSET')].number,
                                            "shiny": reward["pokemonEncounter"].get("pokemonDisplay", {}).get("shiny", False),
                                        }

                                    rewards.append({
                                        "type": rewardtype,
                                        "info": info,
                                    })

                                wh_quest.update({
                                    "rewards": rewards
                                })

                                for condition in quest_json.get('goal', {}).get('condition', []):
                                    conditiontype = _QUESTCONDITION_CONDITIONTYPE.values_by_name[condition.get('type', "UNSET")].number
                                    condition_dict = {
                                        "type": conditiontype,
                                    }

                                    info = {}

                                    if conditiontype == 1:
                                        pokemontype = condition.get('withPokemonType', {}).get('pokemonType', ["POKEMON_TYPE_NONE"])
                                        types = []
                                        for poketype in pokemontype:
                                            types.append(_POKEMONTYPE.values_by_name[poketype].number)
                                        info = {
                                            "pokemon_type_ids": types
                                        }
                                    elif conditiontype == 2:
                                        pokemonids = condition.get('withPokemonCategory', {}).get('pokemonIds', ["MISSINGNO"])
                                        ids = []
                                        for pokemonid in pokemonids:
                                            ids.append(_POKEMONID.values_by_name[pokemonid].number)
                                        info = {
                                            "pokemon_ids": ids
                                        }
                                    elif conditiontype == 7:
                                        raidLevel = condition.get('withRaidLevel', {}).get('raidLevel', ['RAID_LEVEL_UNSET'])
                                        raidlevels = []
                                        for level in raidLevel:
                                            raidlevels.append(_RAIDLEVEL.values_by_name[level].number)
                                        info = {
                                            "raid_levels": raidlevels
                                        }
                                    elif conditiontype == 8:
                                        throwType = condition.get('withThrowType', {}).get('throwType', 'ACTIVITY_UNKNOWN')
                                        info = {
                                            "throw_type_id": _ACTIVITYTYPE.values_by_name[throwType].number
                                        }
                                    elif conditiontype == 11:
                                        item = condition.get('withItem', {})
                                        if item:
                                            info = {
                                                "item_id": _ITEMID.values_by_name[item.get('item', 'ITEM_UNKNOWN')].number
                                            }
                                    elif conditiontype == 14:
                                        throwType = condition.get('withThrowType', {}).get('throwType', 'ACTIVITY_UNKNOWN')
                                        info = {
                                            "throw_type_id": _ACTIVITYTYPE.values_by_name[throwType].number
                                        }

                                    if info:
                                        condition_dict["info"] = info

                                    conditions.append(condition_dict)

                                wh_quest.update({
                                    "conditions": conditions
                                })

                                self.wh_update_queue.put(('quest', wh_quest))
                        except:
                            continue

            if "EncounterResponse" in proto and int(trainerlvl) >= 30:
                encounter_response_string = b64decode(proto['EncounterResponse'])
                encounter = EncounterResponse()
                try:
                    encounter.ParseFromString(encounter_response_string)
                    encounter_response_json = json.loads(MessageToJson(encounter))
                except:
                    continue

                if "wildPokemon" in encounter_response_json:
                    wildpokemon = encounter_response_json["wildPokemon"]

                    if "pokemonData" not in wildpokemon:
                        continue

                    spawn_id = wildpokemon['spawnPointId']

                    sp = SpawnPoint.get_by_id(spawn_id, wildpokemon['latitude'], wildpokemon['longitude'])
                    sp['last_scanned'] = datetime.utcnow()
                    spawn_points[spawn_id] = sp
                    sp['missed_count'] = 0

                    sighting = {
                        'encounter_id': long(wildpokemon['encounterId']),
                        'spawnpoint_id': spawn_id,
                        'scan_time': now_date,
                        'tth_secs': None
                    }

                    # Keep a list of sp_ids to return.
                    sp_id_list.append(spawn_id)

                    # time_till_hidden_ms was overflowing causing a negative integer.
                    # It was also returning a value above 3.6M ms.
                    if 0 < long(wildpokemon.get('timeTillHiddenMs', -1)) < 3600000:
                        d_t_secs = date_secs(datetime.utcfromtimestamp(
                            now() + long(wildpokemon['timeTillHiddenMs']) / 1000.0))

                        # Cover all bases, make sure we're using values < 3600.
                        # Warning: python uses modulo as the least residue, not as
                        # remainder, so we don't apply it to the result.
                        residue_unseen = sp['earliest_unseen'] % 3600
                        residue_seen = sp['latest_seen'] % 3600

                        if (residue_seen != residue_unseen or not sp['last_scanned']):
                            log.info('TTH found for spawnpoint %s.', sp['id'])
                            sighting['tth_secs'] = d_t_secs

                            # Only update when TTH is seen for the first time.
                            # Just before Pokemon migrations, Niantic sets all TTH
                            # to the exact time of the migration, not the normal
                            # despawn time.
                            sp['latest_seen'] = d_t_secs
                            sp['earliest_unseen'] = d_t_secs

                    scan_spawn_points[len(scan_spawn_points) + 1] = {
                        'spawnpoint': sp['id'],
                        'scannedlocation': scan_location['cellid']}
                    if not sp['last_scanned']:
                        log.info('New Spawn Point found.')
                        new_spawn_points.append(sp)

                    if (not SpawnPoint.tth_found(sp) or sighting['tth_secs']):
                        SpawnpointDetectionData.classify(sp, scan_location, now_secs,
                                                         sighting)
                        sightings[long(wildpokemon['encounterId'])] = sighting

                    sp['last_scanned'] = datetime.utcnow()

                    start_end = SpawnPoint.start_end(sp, 1)
                    seconds_until_despawn = (start_end[1] - now_secs) % 3600
                    disappear_time = now_date + timedelta(seconds=seconds_until_despawn)

                    pokemon_id = _POKEMONID.values_by_name[wildpokemon['pokemonData']['pokemonId']].number

                    gender = _GENDER.values_by_name[wildpokemon['pokemonData']["pokemonDisplay"].get('gender', 'GENDER_UNSET')].number
                    costume = _COSTUME.values_by_name[wildpokemon['pokemonData']["pokemonDisplay"].get('costume', 'COSTUME_UNSET')].number
                    form = _FORM.values_by_name[wildpokemon['pokemonData']["pokemonDisplay"].get('form', 'FORM_UNSET')].number
                    weather_boosted_condition = _WEATHERCONDITION.values_by_name[wildpokemon['pokemonData']["pokemonDisplay"].get('weatherBoostedCondition', 'NONE')].number

                    printPokemon(pokemon_id, wildpokemon['latitude'], wildpokemon['longitude'],
                                 disappear_time)

                    pokemon[long(wildpokemon['encounterId'])] = {
                        'encounter_id': long(wildpokemon['encounterId']),
                        'spawnpoint_id': spawn_id,
                        'pokemon_id': pokemon_id,
                        'latitude': wildpokemon['latitude'],
                        'longitude': wildpokemon['longitude'],
                        'disappear_time': disappear_time,
                        'individual_attack': wildpokemon['pokemonData'].get('individualAttack', 0),
                        'individual_defense': wildpokemon['pokemonData'].get('individualDefense', 0),
                        'individual_stamina': wildpokemon['pokemonData'].get('individualStamina', 0),
                        'move_1': _POKEMONMOVE.values_by_name[wildpokemon['pokemonData'].get('move1', 'MOVE_UNSET')].number,
                        'move_2': _POKEMONMOVE.values_by_name[wildpokemon['pokemonData'].get('move2', 'MOVE_UNSET')].number,
                        'cp': wildpokemon['pokemonData'].get('cp', None),
                        'cp_multiplier': wildpokemon['pokemonData'].get('cpMultiplier', None),
                        'height': wildpokemon['pokemonData'].get('heightM', None),
                        'weight': wildpokemon['pokemonData'].get('weightKg', None),
                        'gender': gender,
                        'costume': costume,
                        'form': form,
                        'weather_boosted_condition': weather_boosted_condition
                    }

                    if 'devices' in args.wh_types:
                        if wildpokemon['pokemonData']["pokemonDisplay"].get("shiny", False):
                            wh_worker = {
                                'uuid': deviceworker['deviceid'],
                                'name': deviceworker['name'],
                                'type': 'shiny_pokemon',
                                'message': 'A Shiny Pokemon was discovered for this device at (' + str(wildpokemon['latitude']) + ',' + str(wildpokemon['longitude']) + ').'
                            }
                            self.wh_update_queue.put(('devices', wh_worker))

                    if 'pokemon-iv' in args.wh_types:
                        if (pokemon_id in args.webhook_whitelist or
                            (not args.webhook_whitelist and pokemon_id
                             not in args.webhook_blacklist)):
                            wh_poke = pokemon[long(wildpokemon['encounterId'])].copy()
                            wh_poke.update({
                                'disappear_time': calendar.timegm(
                                    disappear_time.timetuple()),
                                'last_modified_time': now(),
                                'time_until_hidden_ms': float(wildpokemon.get('timeTillHiddenMs', 0)),
                                'verified': SpawnPoint.tth_found(sp),
                                'seconds_until_despawn': seconds_until_despawn,
                                'spawn_start': start_end[0],
                                'spawn_end': start_end[1],
                                'player_level': int(trainerlvl),
                                'individual_attack': wildpokemon['pokemonData'].get('individualAttack', 0),
                                'individual_defense': wildpokemon['pokemonData'].get('individualDefense', 0),
                                'individual_stamina': wildpokemon['pokemonData'].get('individualStamina', 0),
                                'move_1': _POKEMONMOVE.values_by_name[wildpokemon['pokemonData'].get('move1', 'MOVE_UNSET')].number,
                                'move_2': _POKEMONMOVE.values_by_name[wildpokemon['pokemonData'].get('move2', 'MOVE_UNSET')].number,
                                'cp': wildpokemon['pokemonData'].get('cp', 0),
                                'cp_multiplier': wildpokemon['pokemonData'].get('cpMultiplier', 0),
                                'height': wildpokemon['pokemonData'].get('heightM', 0),
                                'weight': wildpokemon['pokemonData'].get('weightKg', 0),
                                'pokemon_level': calc_pokemon_level(wildpokemon['pokemonData'].get('cpMultiplier', 0)),
                                'weather_id': weather_boosted_condition,
                                'expire_timestamp_verified': SpawnPoint.tth_found(sp)
                            })

                            rarity = self.get_pokemon_rarity_code(pokemon_id)
                            wh_poke.update({
                                'rarity': rarity
                            })

                            self.wh_update_queue.put(('pokemon', wh_poke))

        log.info('Parsing found Pokemon: %d (%d skipped), nearby: %d (%d skipped), ' +
                 'pokestops: %d, gyms: %d, raids: %d, quests: %d.',
                 len(pokemon) + pokemon_skipped,
                 pokemon_skipped,
                 len(nearby_pokemons) + nearby_skipped,
                 nearby_skipped,
                 len(pokestops),
                 len(gyms),
                 len(raids),
                 len(quest_result))

        self.db_update_queue.put((ScannedLocation, {0: scan_location}))

        if pokemon:
            self.db_update_queue.put((Pokemon, pokemon))
        if pokestops:
            self.db_update_queue.put((Pokestop, pokestops))
        if pokestop_details:
            self.db_update_queue.put((PokestopDetails, pokestop_details))
        if gyms:
            self.db_update_queue.put((Gym, gyms))
        if gym_details:
            self.db_update_queue.put((GymDetails, gym_details))
        if raids:
            self.db_update_queue.put((Raid, raids))
        if spawn_points:
            self.db_update_queue.put((SpawnPoint, spawn_points))
            self.db_update_queue.put((ScanSpawnPoint, scan_spawn_points))
            if sightings:
                self.db_update_queue.put((SpawnpointDetectionData, sightings))
        if weather:
            self.db_update_queue.put((Weather, weather))
        if nearby_pokemons:
            self.db_update_queue.put((PokestopMember, nearby_pokemons))
        if quest_result:
            self.db_update_queue.put((Quest, quest_result))

        if gym_pokemon:
            self.db_update_queue.put((GymPokemon, gym_pokemon))

        if gym_encountered:
            with GymMember.database().execution_context():
                DeleteQuery(GymMember).where(
                    GymMember.gym_id << gym_encountered.keys()).execute()

        if gym_members:
            self.db_update_queue.put((GymMember, gym_members))

        return 'ok'

    def submit_token(self):
        response = 'error'
        if request.form:
            token = request.form.get('token')
            query = Token.insert(token=token, last_updated=datetime.utcnow())
            query.execute()
            response = 'ok'
        r = make_response(response)
        r.headers.add('Access-Control-Allow-Origin', '*')
        return r

    def validate_request(self):
        args = get_args()

        # Get real IP behind trusted reverse proxy.
        ip_addr = request.remote_addr
        if ip_addr in args.trusted_proxies:
            ip_addr = request.headers.get('X-Forwarded-For', ip_addr)

        # Make sure IP isn't blacklisted.
        if self._ip_is_blacklisted(ip_addr):
            log.debug('Denied access to %s: blacklisted IP.', ip_addr)
            abort(403)

        # Verify user authentication.
        if not args.user_auth:
            return
        if request.endpoint == 'auth_callback':
            return
        if request.endpoint == 'submit_token':
            return
        if request.endpoint == 'get_account_stats':
            return

        return self.discord_api.check_auth(
            session, request.headers.get('User-Agent'), ip_addr)

    def _ip_is_blacklisted(self, ip):
        if not self.blacklist:
            return False

        # Get the nearest IP range
        pos = max(bisect_left(self.blacklist_keys, ip) - 1, 0)
        ip_range = self.blacklist[pos]

        start = dottedQuadToNum(ip_range[0])
        end = dottedQuadToNum(ip_range[1])

        return start <= dottedQuadToNum(ip) <= end

    def set_control_flags(self, control):
        self.control_flags = control

    def set_heartbeat_control(self, heartb):
        self.heartbeat = heartb

    def set_location_queue(self, queue):
        self.location_queue = queue

    def set_current_location(self, location):
        self.current_location = location

    def get_search_control(self):
        return jsonify({
            'status': not self.control_flags['search_control'].is_set()})

    def post_search_control(self):
        args = get_args()
        if not args.search_control or args.on_demand_timeout > 0:
            return 'Search control is disabled', 403
        action = request.args.get('action', 'none')
        if action == 'on':
            self.control_flags['search_control'].clear()
            log.info('Search thread resumed')
        elif action == 'off':
            self.control_flags['search_control'].set()
            log.info('Search thread paused')
        else:
            return jsonify({'message': 'invalid use of api'})
        return self.get_search_control()

    def fullmap(self):
        self.heartbeat[0] = now()
        args = get_args()
        if args.on_demand_timeout > 0:
            self.control_flags['on_demand'].clear()

        search_display = False
        scan_display = False

        visibility_flags = {
            'geofences': bool(not args.no_geofences and (args.geofence_file or
                              args.geofence_excluded_file)),
            'gyms': not args.no_gyms,
            'pokemons': not args.no_pokemon,
            'pokestops': not args.no_pokestops,
            'raids': not args.no_raids,
            'gym_info': args.gym_info,
            'encounter': False,
            'scan_display': scan_display,
            'search_display': search_display,
            'fixed_display': True,
            'custom_css': args.custom_css,
            'custom_js': args.custom_js,
            'devices': not args.no_devices,
            'find_pane': not args.no_find_pane,
            'show_auth': True if args.user_auth else False
        }

        map_lat = self.current_location[0]
        map_lng = self.current_location[1]

        geofences = request.args.get('geofences', args.default_geofence)
        if not self.geofences:
            from .geofence import Geofences
            self.geofences = Geofences()
        if geofences != '' and self.geofences.is_enabled():
            swLat, swLng, neLat, neLng = self.geofences.get_boundary_coords(geofences)
            map_lat = (swLat + neLat) / 2
            map_lng = (swLng + neLng) / 2

        return render_template('map.html',
                               lat=map_lat,
                               lng=map_lng,
                               gmaps_key=args.gmaps_key,
                               lang=args.locale,
                               show=visibility_flags,
                               mapname=args.mapname,
                               generateImages=str(args.generate_images).lower(),
                               geofences=str(geofences),
                               analyticskey=str(args.google_analytics_key),
                               usegoogleanalytics=True if str(args.google_analytics_key) != '' else False,
                               )

    def raidview(self):
        self.heartbeat[0] = now()
        args = get_args()
        if args.on_demand_timeout > 0:
            self.control_flags['on_demand'].clear()

        if args.no_raids_page:
            abort(404)

        map_lat = self.current_location[0]
        map_lng = self.current_location[1]

        geofences = request.args.get('geofences', args.default_geofence)

        return render_template('raids.html',
                               lat=map_lat,
                               lng=map_lng,
                               mapname=args.mapname,
                               lang=args.locale,
                               geofences=str(geofences),
                               analyticskey=str(args.google_analytics_key),
                               usegoogleanalytics=True if str(args.google_analytics_key) != '' else False,
                               )

    def devicesview(self):
        self.heartbeat[0] = now()
        args = get_args()
        if args.on_demand_timeout > 0:
            self.control_flags['on_demand'].clear()

        if args.no_devices_page:
            abort(404)

        map_lat = self.current_location[0]
        map_lng = self.current_location[1]
        needlogin = True
        if not args.devices_page_accounts:
            needlogin = False

        geofences = request.args.get('geofences', args.default_geofence)

        return render_template('devices.html',
                               lat=map_lat,
                               lng=map_lng,
                               mapname=args.mapname,
                               lang=args.locale,
                               needlogin=str(needlogin).lower(),
                               geofences=str(geofences),
                               analyticskey=str(args.google_analytics_key),
                               usegoogleanalytics=True if str(args.google_analytics_key) != '' else False,
                               )

    def questview(self):
        self.heartbeat[0] = now()
        args = get_args()
        if args.on_demand_timeout > 0:
            self.control_flags['on_demand'].clear()

        if args.no_quests_page:
            abort(404)

        map_lat = self.current_location[0]
        map_lng = self.current_location[1]

        geofences = request.args.get('geofences', args.default_geofence)

        return render_template('quests.html',
                               lat=map_lat,
                               lng=map_lng,
                               mapname=args.mapname,
                               lang=args.locale,
                               geofences=str(geofences),
                               analyticskey=str(args.google_analytics_key),
                               usegoogleanalytics=True if str(args.google_analytics_key) != '' else False,
                               )

    def raw_data(self):
        # Make sure fingerprint isn't blacklisted.
        fingerprint_blacklisted = any([
            fingerprints['no_referrer'](request),
            fingerprints['iPokeGo'](request)
        ])

        if fingerprint_blacklisted:
            log.debug('User denied access: blacklisted fingerprint.')
            abort(403)

        self.heartbeat[0] = now()
        args = get_args()
        if args.on_demand_timeout > 0:
            self.control_flags['on_demand'].clear()
        d = {}

        # Request time of this request.
        d['timestamp'] = datetime.utcnow()

        # Request time of previous request.
        if request.args.get('timestamp'):
            timestamp = int(request.args.get('timestamp'))
            timestamp -= 1000  # Overlap, for rounding errors.
        else:
            timestamp = 0

        swLat = request.args.get('swLat')
        swLng = request.args.get('swLng')
        neLat = request.args.get('neLat')
        neLng = request.args.get('neLng')

        oSwLat = request.args.get('oSwLat')
        oSwLng = request.args.get('oSwLng')
        oNeLat = request.args.get('oNeLat')
        oNeLng = request.args.get('oNeLng')

        # Previous switch settings.
        lastgyms = request.args.get('lastgyms')
        lastpokestops = request.args.get('lastpokestops')
        lastpokemon = request.args.get('lastpokemon')
        lastslocs = request.args.get('lastslocs')
        lastspawns = request.args.get('lastspawns')

        geofencenames = request.args.get('geofencenames', '')

        if request.args.get('luredonly', 'true') == 'true':
            luredonly = True
        else:
            luredonly = False

        # Current switch settings saved for next request.
        if request.args.get('gyms', 'true') == 'true':
            d['lastgyms'] = request.args.get('gyms', 'true')

        if request.args.get('pokestops', 'true') == 'true':
            d['lastpokestops'] = request.args.get('pokestops', 'true')

        if request.args.get('pokemon', 'true') == 'true':
            d['lastpokemon'] = request.args.get('pokemon', 'true')

        if request.args.get('scanned', 'true') == 'true':
            d['lastslocs'] = request.args.get('scanned', 'true')

        if request.args.get('spawnpoints', 'false') == 'true':
            d['lastspawns'] = request.args.get('spawnpoints', 'false')

        # If old coords are not equal to current coords we have moved/zoomed!
        if (oSwLng < swLng and oSwLat < swLat and
                oNeLat > neLat and oNeLng > neLng):
            newArea = False  # We zoomed in no new area uncovered.
        elif not (oSwLat == swLat and oSwLng == swLng and
                  oNeLat == neLat and oNeLng == neLng):
            newArea = True
        else:
            newArea = False

        # Pass current coords as old coords.
        d['oSwLat'] = swLat
        d['oSwLng'] = swLng
        d['oNeLat'] = neLat
        d['oNeLng'] = neLng

        if not self.geofences:
            from .geofence import Geofences
            self.geofences = Geofences()

        if (request.args.get('pokemon', 'true') == 'true' and
                not args.no_pokemon):

            # Exclude ids of Pokemon that are hidden.
            eids = []
            request_eids = request.args.get('eids')
            if request_eids:
                eids = {int(i) for i in request_eids.split(',')}

            if request.args.get('ids'):
                request_ids = request.args.get('ids').split(',')
                ids = [int(x) for x in request_ids if int(x) not in eids]
                d['pokemons'] = convert_pokemon_list(
                    Pokemon.get_active_by_id(ids, swLat, swLng, neLat, neLng))
            elif lastpokemon != 'true':
                # If this is first request since switch on, load
                # all pokemon on screen.
                d['pokemons'] = convert_pokemon_list(
                    Pokemon.get_active(
                        swLat, swLng, neLat, neLng, exclude=eids))
            else:
                # If map is already populated only request modified Pokemon
                # since last request time.
                d['pokemons'] = convert_pokemon_list(
                    Pokemon.get_active(
                        swLat, swLng, neLat, neLng,
                        timestamp=timestamp, exclude=eids))
                if newArea:
                    # If screen is moved add newly uncovered Pokemon to the
                    # ones that were modified since last request time.
                    d['pokemons'] = d['pokemons'] + (
                        convert_pokemon_list(
                            Pokemon.get_active(
                                swLat,
                                swLng,
                                neLat,
                                neLng,
                                exclude=eids,
                                oSwLat=oSwLat,
                                oSwLng=oSwLng,
                                oNeLat=oNeLat,
                                oNeLng=oNeLng)))

            if request.args.get('reids'):
                reids = [int(x) for x in request.args.get('reids').split(',')]
                d['pokemons'] = d['pokemons'] + (
                    convert_pokemon_list(
                        Pokemon.get_active_by_id(reids, swLat, swLng, neLat,
                                                 neLng)))
                d['reids'] = reids

            if len(d['pokemons']) > 0 and (not args.data_outside_geofences or geofencenames != "") and self.geofences.is_enabled():
                d['pokemons'] = self.geofences.get_geofenced_results(d['pokemons'], geofencenames)

        if (request.args.get('pokestops', 'true') == 'true' and
                not args.no_pokestops):
            if lastpokestops != 'true':
                d['pokestops'] = Pokestop.get_stops(swLat, swLng, neLat, neLng,
                                                    lured=luredonly)
            else:
                d['pokestops'] = Pokestop.get_stops(swLat, swLng, neLat, neLng,
                                                    timestamp=timestamp)
                if newArea:
                    d['pokestops'].update(
                        Pokestop.get_stops(swLat, swLng, neLat, neLng,
                                           oSwLat=oSwLat, oSwLng=oSwLng,
                                           oNeLat=oNeLat, oNeLng=oNeLng,
                                           lured=luredonly))
            if len(d['pokestops']) > 0 and (not args.data_outside_geofences or geofencenames != "") and self.geofences.is_enabled():
                d['pokestops'] = self.geofences.get_geofenced_results(d['pokestops'], geofencenames)

        if request.args.get('gyms', 'true') == 'true' and not args.no_gyms:
            if lastgyms != 'true':
                d['gyms'] = Gym.get_gyms(swLat, swLng, neLat, neLng)
            else:
                d['gyms'] = Gym.get_gyms(swLat, swLng, neLat, neLng,
                                         timestamp=timestamp)
                if newArea:
                    d['gyms'].update(
                        Gym.get_gyms(swLat, swLng, neLat, neLng,
                                     oSwLat=oSwLat, oSwLng=oSwLng,
                                     oNeLat=oNeLat, oNeLng=oNeLng))
            if len(d['gyms']) > 0 and (not args.data_outside_geofences or geofencenames != "") and self.geofences.is_enabled():
                d['gyms'] = self.geofences.get_geofenced_results(d['gyms'], geofencenames)

        if request.args.get('seen', 'false') == 'true':
            d['seen'] = Pokemon.get_seen(int(request.args.get('duration')))

        if request.args.get('appearances', 'false') == 'true':
            d['appearances'] = Pokemon.get_appearances(
                request.args.get('pokemonid'),
                int(request.args.get('duration')))

        if request.args.get('appearancesDetails', 'false') == 'true':
            d['appearancesTimes'] = (
                Pokemon.get_appearances_times_by_spawnpoint(
                    request.args.get('pokemonid'),
                    request.args.get('spawnpoint_id'),
                    int(request.args.get('duration'))))

        if request.args.get('spawnpoints', 'false') == 'true':
            if lastspawns != 'true':
                d['spawnpoints'] = SpawnPoint.get_spawnpoints(
                    swLat=swLat, swLng=swLng, neLat=neLat, neLng=neLng)
            else:
                d['spawnpoints'] = SpawnPoint.get_spawnpoints(
                    swLat=swLat, swLng=swLng, neLat=neLat, neLng=neLng,
                    timestamp=timestamp)
                if newArea:
                    d['spawnpoints'] = d['spawnpoints'] + (
                        SpawnPoint.get_spawnpoints(
                            swLat, swLng, neLat, neLng,
                            oSwLat=oSwLat, oSwLng=oSwLng,
                            oNeLat=oNeLat, oNeLng=oNeLng))
            if len(d['spawnpoints']) > 0 and (not args.data_outside_geofences or geofencenames != "") and self.geofences.is_enabled():
                d['spawnpoints'] = self.geofences.get_geofenced_results(d['spawnpoints'], geofencenames)

        if request.args.get('status', 'false') == 'true':
            args = get_args()
            d = {}
            if args.status_page_password is None:
                d['error'] = 'Access denied'
            elif (request.args.get('password', None) ==
                  args.status_page_password):
                max_status_age = args.status_page_filter
                if max_status_age > 0:
                    d['main_workers'] = MainWorker.get_recent(max_status_age)
                    d['workers'] = WorkerStatus.get_recent(max_status_age)
                else:
                    d['main_workers'] = MainWorker.get_all()
                    d['workers'] = WorkerStatus.get_all()

        if request.args.get('weather', 'false') == 'true':
            d['weather'] = get_weather_cells(swLat, swLng, neLat, neLng)

        if request.args.get('s2cells', 'false') == 'true':
            d['s2cells'] = get_s2_coverage(swLat, swLng, neLat, neLng)

        if request.args.get('weatherAlerts', 'false') == 'true':
            d['weatherAlerts'] = get_weather_alerts(swLat, swLng, neLat, neLng)

        if not args.no_geofences and request.args.get('geofences', 'true') == 'true' and self.geofences.is_enabled():
            allgeofences = self.geofences.geofenced_areas
            allexcludedgeofences = self.geofences.excluded_areas

            geofences = {}
            for g in allgeofences:
                # Check if already there
                if geofencenames != '':
                    geofences_to_search_for = geofencenames.lower().split(",")
                    if g['name'].lower() not in geofences_to_search_for:
                        continue
                geofence = geofences.get(g['name'], None)
                if not geofence:  # Create a new sub-dict if new
                    geofences[g['name']] = {
                        'excluded': False,
                        'name': g['name'],
                        'coordinates': []
                    }
                for point in g['polygon']:
                    coordinate = {
                        'lat': point['lat'],
                        'lng': point['lon']
                    }
                    geofences[g['name']]['coordinates'].append(coordinate)

            for g in allexcludedgeofences:
                # Check if already there
                if geofencenames != '':
                    geofences_to_search_for = geofencenames.lower().split(",")
                    if g['name'].lower() not in geofences_to_search_for:
                        continue
                geofence = geofences.get(g['name'], None)
                if not geofence:  # Create a new sub-dict if new
                    geofences[g['name']] = {
                        'excluded': True,
                        'name': g['name'],
                        'coordinates': []
                    }
                for point in g['polygon']:
                    coordinate = {
                        'lat': point['lat'],
                        'lng': point['lon']
                    }
                    geofences[g['name']]['coordinates'].append(coordinate)

            d['geofences'] = geofences

        if not args.no_devices and request.args.get('devices', 'true') == 'true':
            if request.args.get('scanned', 'true') == 'true':
                if lastslocs != 'true':
                    d['scanned'] = ScannedLocation.get_recent(swLat, swLng,
                                                              neLat, neLng)
                else:
                    d['scanned'] = ScannedLocation.get_recent(swLat, swLng,
                                                              neLat, neLng,
                                                              timestamp=timestamp)
                    if newArea:
                        d['scanned'] = d['scanned'] + ScannedLocation.get_recent(
                            swLat, swLng, neLat, neLng, oSwLat=oSwLat,
                            oSwLng=oSwLng, oNeLat=oNeLat, oNeLng=oNeLng)

            # d['deviceworkers'] = DeviceWorker.get_active()
            d['deviceworkers'] = self.get_active_devices()

            if request.args.get('routes', 'true') == 'true':
                routes = {}
                for deviceworker in d['deviceworkers']:
                    uuid = deviceworker['deviceid']
                    if deviceworker['fetching'] != 'IDLE':
                        route = self.deviceschedules.get(uuid, [])
                        if len(route) > 0:
                            routes[uuid] = {
                                'name': uuid,
                                'coordinates': []
                            }
                            coordinate = {
                                'lat': deviceworker['latitude'],
                                'lng': deviceworker['longitude']
                            }
                            routes[uuid]['coordinates'].append(coordinate)
                            for point in route:
                                coordinate = {
                                    'lat': point[0],
                                    'lng': point[1]
                                }
                                routes[uuid]['coordinates'].append(coordinate)

                d['routes'] = routes

        return jsonify(d)

    def raw_raid(self):
        # Make sure fingerprint isn't blacklisted.
        fingerprint_blacklisted = any([
            fingerprints['no_referrer'](request),
            fingerprints['iPokeGo'](request)
        ])

        if fingerprint_blacklisted:
            log.debug('User denied access: blacklisted fingerprint.')
            abort(403)

        self.heartbeat[0] = now()
        args = get_args()
        if args.on_demand_timeout > 0:
            self.control_flags['on_demand'].clear()

        geofencenames = request.form.get('geofencenames', '')

        d = {}
        d['timestamp'] = datetime.utcnow()
        d['raids'] = Gym.get_raids()
        if not self.geofences:
            from .geofence import Geofences
            self.geofences = Geofences()
        if len(d['raids']) > 0 and (not args.data_outside_geofences or geofencenames != "") and self.geofences.is_enabled():
            d['raids'] = self.geofences.get_geofenced_results(d['raids'], geofencenames)
        return jsonify(d)

    def is_devices_user(self, username, password):
        args = get_args()

        if len(self.devices_users) == 0 and args.devices_page_accounts:
            with open(args.devices_page_accounts) as f:
                for line in f:
                    line = line.strip()
                    if len(line) == 0:  # Empty line.
                        continue
                    user, pwd = line.split(":")
                    self.devices_users[user.strip()] = pwd.strip()

        if username in self.devices_users and self.devices_users[username] == password:
            return True

        return False

    def raw_devices(self):
        # Make sure fingerprint isn't blacklisted.
        fingerprint_blacklisted = any([
            fingerprints['no_referrer'](request),
            fingerprints['iPokeGo'](request)
        ])

        if fingerprint_blacklisted:
            log.debug('User denied access: blacklisted fingerprint.')
            abort(403)

        self.heartbeat[0] = now()
        args = get_args()
        if args.on_demand_timeout > 0:
            self.control_flags['on_demand'].clear()

        geofencenames = request.form.get('geofencenames', '')

        d = {}

        d['timestamp'] = datetime.utcnow()
        enteredusername = request.form.get('username', None)
        enteredpassword = request.form.get('password', None)
        if not args.devices_page_accounts or self.is_devices_user(enteredusername, enteredpassword):
            if not args.devices_page_accounts:
                enteredusername = 'admin'
            d['login'] = 'ok'
            d['admin'] = enteredusername == 'admin'
            d['devices'] = []
            if not args.no_devices:
                active_devices = self.get_active_devices()

                for deviceworker in active_devices:
                    if enteredusername == 'admin' or enteredusername == deviceworker['username']:
                        d['devices'].append(deviceworker)
                        uuid = deviceworker['deviceid']
                        deviceworker['route'] = 0
                        if deviceworker['fetching'] != 'IDLE' and uuid in self.deviceschedules:
                            deviceworker['route'] = len(self.deviceschedules[uuid])
            if len(d['devices']) > 0 and (not args.data_outside_geofences or geofencenames != "") and self.geofences.is_enabled():
                d['devices'] = self.geofences.get_geofenced_results(d['devices'], geofencenames)

        else:
            d['login'] = 'failed'

        return jsonify(d)

    def raw_quests(self):
        # Make sure fingerprint isn't blacklisted.
        fingerprint_blacklisted = any([
            fingerprints['no_referrer'](request),
            fingerprints['iPokeGo'](request)
        ])

        if fingerprint_blacklisted:
            log.debug('User denied access: blacklisted fingerprint.')
            abort(403)

        self.heartbeat[0] = now()
        args = get_args()
        if args.on_demand_timeout > 0:
            self.control_flags['on_demand'].clear()

        geofencenames = request.form.get('geofencenames', '')

        d = {}
        d['timestamp'] = datetime.utcnow()

        if not self.geofences:
            from .geofence import Geofences
            self.geofences = Geofences()
        if self.geofences.is_enabled():
            swLat, swLng, neLat, neLng = self.geofences.get_boundary_coords(geofencenames)
        else:
            swLat = None
            swLng = None
            neLat = None
            neLng = None

        d['quests'] = Quest.get_quests(swLat, swLng, neLat, neLng)

        if len(d['quests']) > 0 and (not args.data_outside_geofences or geofencenames != "") and self.geofences.is_enabled():
            d['quests'] = self.geofences.get_geofenced_results(d['quests'], geofencenames)

        return jsonify(d)

    def loc(self):
        d = {}
        d['lat'] = self.current_location[0]
        d['lng'] = self.current_location[1]

        return jsonify(d)

    def get_gpx_route(self, routename):
        result = []

        gpx_file = open(routename, 'r')

        gpx = gpxpy.parse(gpx_file)

        for track in gpx.tracks:
            for segment in track.segments:
                for point in segment.points:
                    result.append((round(point.latitude, 5), round(point.longitude, 5)))

        for waypoint in gpx.waypoints:
            result.append((round(waypoint.latitude, 5), round(waypoint.longitude, 5)))

        for route in gpx.routes:
            for point in route.points:
                result.append((round(point.latitude, 5), round(point.longitude, 5)))

        return result

    def changeDeviceLoc(self, lat, lon, uuid):
        canusedevice, devicename = self.trusted_device(uuid)
        if not canusedevice:
            return ""

        deviceworker = DeviceWorker.get_existing_by_id(uuid)

        if not deviceworker or (not deviceworker['last_scanned'] and deviceworker['fetching'] == 'IDLE'):
            return "Not moved, device isn't found or has never scanned and isn't fetching"

        if uuid in self.deviceschedules:
            self.deviceschedules[uuid] = []

        if uuid in self.devicesscheduling:
            self.devicesscheduling.remove(uuid)

        deviceworker['latitude'] = round(lat, 5)
        deviceworker['longitude'] = round(lon, 5)
        deviceworker['last_updated'] = datetime.utcnow()
        deviceworker['fetching'] = "jump_now"

        if devicename != "" and devicename != deviceworker['name']:
            deviceworker['name'] = devicename

        self.save_device(deviceworker)

        d = {}
        d['latitude'] = deviceworker['latitude']
        d['longitude'] = deviceworker['longitude']

        return jsonify(d)

    def walk_spawnpoint(self, mapcontrolled, uuid, latitude, longitude, request_json):
        args = get_args()

        if latitude == 0 and longitude == 0:
            latitude = self.current_location[0]
            longitude = self.current_location[1]

        deviceworker = self.get_device(uuid, latitude, longitude)

        self.track_event('fetch', 'walk_spawnpoint', uuid)

        if uuid not in self.deviceschedules:
            self.deviceschedules[uuid] = []

        if deviceworker['fetching'] == "jump_now":
            deviceworker['last_updated'] = datetime.utcnow()
            deviceworker['fetching'] = "walk_spawnpoint"

            self.save_device(deviceworker)

            d = {}
            d['latitude'] = deviceworker['latitude']
            d['longitude'] = deviceworker['longitude']

            return jsonify(d)

        if uuid in self.devicesscheduling:
            if len(self.deviceschedules[uuid]) == 0:
                d = {}
                d['latitude'] = deviceworker['latitude']
                d['longitude'] = deviceworker['longitude']

                return jsonify(d)
            else:
                self.devicesscheduling.remove(uuid)

        last_updated = deviceworker['last_updated']
        difference = (datetime.utcnow() - last_updated).total_seconds()

        scheduletimeout = request_json.get('scheduletimeout', args.scheduletimeout)
        maxradius = request_json.get('maxradius', args.maxradius)
        stepsize = request_json.get('stepsize', args.stepsize)
        unknown_tth = request_json.get('unknown_tth', False)
        maxpoints = request_json.get('maxpoints', False)
        geofence = request_json.get('geofence', "")
        no_overlap = request_json.get('no_overlap', False)
        speed = request_json.get('speed', args.speed)
        arrived_range = request_json.get('arrived_range', args.arrived_range)

        if not isinstance(scheduletimeout, (int, long)):
            try:
                scheduletimeout = int(scheduletimeout)
            except:
                pass
        if not isinstance(maxradius, (int, long)):
            try:
                maxradius = int(maxradius)
            except:
                pass
        if not isinstance(stepsize, (float)):
            try:
                stepsize = int(stepsize)
            except:
                pass
        if not isinstance(unknown_tth, (bool, int, long)):
            try:
                if unknown_tth.lower() == 'true':
                    unknown_tth = True
                elif unknown_tth.lower() == 'false':
                    unknown_tth = False
                else:
                    unknown_tth = int(unknown_tth)
            except:
                pass
        if not isinstance(maxpoints, (bool, int, long)):
            try:
                if maxpoints.lower() == 'true':
                    maxpoints = True
                elif maxpoints.lower() == 'false':
                    maxpoints = False
                else:
                    maxpoints = int(maxpoints)
            except:
                pass
        if not isinstance(no_overlap, bool):
            try:
                if no_overlap.lower() == 'true':
                    no_overlap = True
                else:
                    no_overlap = False
            except:
                pass
        if not isinstance(mapcontrolled, bool):
            try:
                if mapcontrolled.lower() == 'true':
                    mapcontrolled = True
                else:
                    mapcontrolled = False
            except:
                pass
        if not isinstance(speed, (int, long)):
            try:
                speed = int(speed)
            except:
                pass
        if not isinstance(arrived_range, (int, long)):
            try:
                arrived_range = int(arrived_range)
            except:
                pass

        deviceworker['no_overlap'] = no_overlap
        deviceworker['mapcontrolled'] = mapcontrolled

        if (deviceworker['fetching'] == 'IDLE' and difference > scheduletimeout * 60) or (deviceworker['fetching'] != 'IDLE' and deviceworker['fetching'] != "walk_spawnpoint"):
            self.deviceschedules[uuid] = []

        if len(self.deviceschedules[uuid]) > 0:
            nexttarget = self.deviceschedules[uuid][0]

            distance_m = geopy.distance.vincenty((latitude, longitude), (nexttarget[0], nexttarget[1])).meters

            if distance_m <= arrived_range:
                if len(self.deviceschedules[uuid]) > 0:
                    del self.deviceschedules[uuid][0]

        if len(self.deviceschedules[uuid]) == 0:
            scheduled_points = []
            if no_overlap:
                for dev in self.get_active_devices():
                    if dev.get('no_overlap') and dev['fetching'] == 'walk_spawnpoint':
                        if dev['deviceid'] in self.devicesscheduling:
                            d = {}
                            d['latitude'] = deviceworker['latitude']
                            d['longitude'] = deviceworker['longitude']
                            return jsonify(d)

                        scheduled_points += [item[2] for item in self.deviceschedules[dev['deviceid']]]

            self.devicesscheduling.append(uuid)

            if not self.geofences:
                from .geofence import Geofences
                self.geofences = Geofences()

            self.deviceschedules[uuid] = SpawnPoint.get_nearby_spawnpoints(latitude, longitude, maxradius, unknown_tth, maxpoints, geofence, scheduled_points, self.geofences)
            nextlatitude = latitude
            nextlongitude = longitude
            if unknown_tth and len(self.deviceschedules[uuid]) == 0:
                self.deviceschedules[uuid] = SpawnPoint.get_nearby_spawnpoints(latitude, longitude, maxradius, False, maxpoints, geofence, scheduled_points, self.geofences)
            if len(self.deviceschedules[uuid]) == 0:
                return self.scan_loc(mapcontrolled, uuid, latitude, longitude, request_json)
        else:
            nextlatitude = deviceworker['latitude']
            nextlongitude = deviceworker['longitude']

        nexttarget = self.deviceschedules[uuid][0]

        dlat = abs(nexttarget[0] - nextlatitude)
        dlong = abs(nexttarget[1] - nextlongitude)

        distance_m = geopy.distance.vincenty((nextlatitude, nextlongitude), (nexttarget[0], nexttarget[1])).meters
        num_seconds = distance_m / speed * 3.6  # 7.6 = 2x walk speed
        # log.info("{} - Distance to go: {} metres, Time until arrival: {} seconds".format(deviceworker['name'], distance_m, num_seconds))

        if num_seconds <= 1.0:
            nextlatitude = nexttarget[0]
            nextlongitude = nexttarget[1]
        else:
            dlat_per_second = dlat / num_seconds
            dlong_per_second = dlong / num_seconds

            if nextlatitude < nexttarget[0]:
                nextlatitude += dlat_per_second
            else:
                nextlatitude -= dlat_per_second

            if nextlongitude < nexttarget[1]:
                nextlongitude += dlong_per_second
            else:
                nextlongitude -= dlong_per_second

        deviceworker['latitude'] = round(nextlatitude, 5)
        deviceworker['longitude'] = round(nextlongitude, 5)
        deviceworker['last_updated'] = datetime.utcnow()
        deviceworker['fetching'] = "walk_spawnpoint"

        self.save_device(deviceworker)

        d = {}
        d['latitude'] = deviceworker['latitude']
        d['longitude'] = deviceworker['longitude']

        return jsonify(d)

    def walk_gpx(self, mapcontrolled, uuid, latitude, longitude, request_json):
        args = get_args()

        if latitude == 0 and longitude == 0:
            latitude = self.current_location[0]
            longitude = self.current_location[1]

        deviceworker = self.get_device(uuid, latitude, longitude)

        self.track_event('fetch', 'walk_gpx', uuid)

        if uuid not in self.deviceschedules:
            self.deviceschedules[uuid] = []

        if deviceworker['fetching'] == "jump_now":
            deviceworker['last_updated'] = datetime.utcnow()
            deviceworker['fetching'] = "walk_gpx"

            self.save_device(deviceworker)

            d = {}
            d['latitude'] = deviceworker['latitude']
            d['longitude'] = deviceworker['longitude']

            return jsonify(d)

        if uuid in self.devicesscheduling:
            if len(self.deviceschedules[uuid]) == 0:
                d = {}
                d['latitude'] = deviceworker['latitude']
                d['longitude'] = deviceworker['longitude']

                return jsonify(d)
            else:
                self.devicesscheduling.remove(uuid)

        scheduletimeout = request_json.get('scheduletimeout', args.scheduletimeout)
        stepsize = request_json.get('stepsize', args.stepsize)
        speed = request_json.get('speed', args.speed)
        arrived_range = request_json.get('arrived_range', args.arrived_range)

        if not isinstance(scheduletimeout, (int, long)):
            try:
                scheduletimeout = int(scheduletimeout)
            except:
                pass
        if not isinstance(stepsize, (float)):
            try:
                stepsize = int(stepsize)
            except:
                pass
        if not isinstance(mapcontrolled, bool):
            try:
                if mapcontrolled.lower() == 'true':
                    mapcontrolled = True
                else:
                    mapcontrolled = False
            except:
                pass
        if not isinstance(speed, (int, long)):
            try:
                speed = int(speed)
            except:
                pass
        if not isinstance(arrived_range, (int, long)):
            try:
                arrived_range = int(arrived_range)
            except:
                pass

        deviceworker['mapcontrolled'] = mapcontrolled
        deviceworker['no_overlap'] = False

        last_updated = deviceworker['last_updated']
        difference = (datetime.utcnow() - last_updated).total_seconds()
        if (deviceworker['fetching'] == 'IDLE' and difference > scheduletimeout * 60) or (deviceworker['fetching'] != 'IDLE' and deviceworker['fetching'] != "walk_gpx"):
            self.deviceschedules[uuid] = []

        routename = request_json.get('route', "", type=str)
        if routename is None or routename == "":
            routename = uuid

        if (deviceworker['fetching'] == 'walk_gpx' and deviceworker.get('route', '') != routename and deviceworker.get('route', '') != ''):
            self.deviceschedules[uuid] = []

        if len(self.deviceschedules[uuid]) > 0:
            nexttarget = self.deviceschedules[uuid][0]

            distance_m = geopy.distance.vincenty((latitude, longitude), (nexttarget[0], nexttarget[1])).meters

            if distance_m <= arrived_range:
                if len(self.deviceschedules[uuid]) > 0:
                    del self.deviceschedules[uuid][0]

        if len(self.deviceschedules[uuid]) == 0:
            gpxfilename = ""
            if routename != "":
                gpxfilename = os.path.join(
                    args.root_path,
                    'gpx',
                    routename + ".gpx")
            if gpxfilename == "" or not os.path.isfile(gpxfilename):
                log.warning("No or incorrect GPX supplied: {}".format(gpxfilename))
                return self.scan_loc(mapcontrolled, uuid, latitude, longitude, request_json)

            self.devicesscheduling.append(uuid)
            self.deviceschedules[uuid] = self.get_gpx_route(gpxfilename)
            if len(self.deviceschedules[uuid]) == 0:
                return self.scan_loc(mapcontrolled, uuid, latitude, longitude, request_json)
            deviceworker['route'] = routename
            self.save_device(deviceworker)

        nextlatitude = deviceworker['latitude']
        nextlongitude = deviceworker['longitude']

        nexttarget = self.deviceschedules[uuid][0]

        dlat = abs(nexttarget[0] - nextlatitude)
        dlong = abs(nexttarget[1] - nextlongitude)

        distance_m = geopy.distance.vincenty((nextlatitude, nextlongitude), (nexttarget[0], nexttarget[1])).meters
        num_seconds = distance_m / speed * 3.6  # 7.6 = 2x walk speed
        # log.info("{} - Distance to go: {} metres, Time until arrival: {} seconds".format(deviceworker['name'], distance_m, num_seconds))

        if num_seconds <= 1.0:
            nextlatitude = nexttarget[0]
            nextlongitude = nexttarget[1]
        else:
            dlat_per_second = dlat / num_seconds
            dlong_per_second = dlong / num_seconds

            if nextlatitude < nexttarget[0]:
                nextlatitude += dlat_per_second
            else:
                nextlatitude -= dlat_per_second

            if nextlongitude < nexttarget[1]:
                nextlongitude += dlong_per_second
            else:
                nextlongitude -= dlong_per_second

        deviceworker['latitude'] = round(nextlatitude, 5)
        deviceworker['longitude'] = round(nextlongitude, 5)
        deviceworker['last_updated'] = datetime.utcnow()
        deviceworker['fetching'] = "walk_gpx"

        self.save_device(deviceworker)

        d = {}
        d['latitude'] = deviceworker['latitude']
        d['longitude'] = deviceworker['longitude']

        return jsonify(d)

    def walk_pokestop(self, mapcontrolled, uuid, latitude, longitude, request_json):
        args = get_args()

        if latitude == 0 and longitude == 0:
            latitude = self.current_location[0]
            longitude = self.current_location[1]

        deviceworker = self.get_device(uuid, latitude, longitude)

        self.track_event('fetch', 'walk_pokestop', uuid)

        if uuid not in self.deviceschedules:
            self.deviceschedules[uuid] = []

        if deviceworker['fetching'] == "jump_now":
            deviceworker['last_updated'] = datetime.utcnow()
            deviceworker['fetching'] = "walk_pokestop"

            self.save_device(deviceworker)

            d = {}
            d['latitude'] = deviceworker['latitude']
            d['longitude'] = deviceworker['longitude']

            return jsonify(d)

        if uuid in self.devicesscheduling:
            if len(self.deviceschedules[uuid]) == 0:
                d = {}
                d['latitude'] = deviceworker['latitude']
                d['longitude'] = deviceworker['longitude']

                return jsonify(d)
            else:
                self.devicesscheduling.remove(uuid)

        scheduletimeout = request_json.get('scheduletimeout', args.scheduletimeout)
        maxradius = request_json.get('maxradius', args.maxradius)
        stepsize = request_json.get('stepsize', args.stepsize)
        questless = request_json.get('questless', False)
        maxpoints = request_json.get('maxpoints', False)
        geofence = request_json.get('geofence', "")
        no_overlap = request_json.get('no_overlap', False)
        speed = request_json.get('speed', args.speed)
        arrived_range = request_json.get('arrived_range', args.arrived_range)

        if not isinstance(scheduletimeout, (int, long)):
            try:
                scheduletimeout = int(scheduletimeout)
            except:
                pass
        if not isinstance(maxradius, (int, long)):
            try:
                maxradius = int(maxradius)
            except:
                pass
        if not isinstance(stepsize, (float)):
            try:
                stepsize = int(stepsize)
            except:
                pass
        if not isinstance(questless, (bool, int, long)):
            try:
                if questless.lower() == 'true':
                    questless = True
                elif questless.lower() == 'false':
                    questless = False
                else:
                    questless = int(questless)
            except:
                pass
        if not isinstance(maxpoints, (bool, int, long)):
            try:
                if maxpoints.lower() == 'true':
                    maxpoints = True
                elif maxpoints.lower() == 'false':
                    maxpoints = False
                else:
                    maxpoints = int(maxpoints)
            except:
                pass
        if not isinstance(no_overlap, bool):
            try:
                if no_overlap.lower() == 'true':
                    no_overlap = True
                else:
                    no_overlap = False
            except:
                pass
        if not isinstance(speed, (int, long)):
            try:
                speed = int(speed)
            except:
                pass
        if not isinstance(arrived_range, (int, long)):
            try:
                arrived_range = int(arrived_range)
            except:
                pass

        deviceworker['no_overlap'] = no_overlap
        deviceworker['mapcontrolled'] = mapcontrolled

        last_updated = deviceworker['last_updated']
        difference = (datetime.utcnow() - last_updated).total_seconds()
        if (deviceworker['fetching'] == 'IDLE' and difference > scheduletimeout * 60) or (deviceworker['fetching'] != 'IDLE' and deviceworker['fetching'] != "walk_pokestop"):
            self.deviceschedules[uuid] = []

        if len(self.deviceschedules[uuid]) > 0:
            nexttarget = self.deviceschedules[uuid][0]

            distance_m = geopy.distance.vincenty((latitude, longitude), (nexttarget[0], nexttarget[1])).meters

            if distance_m <= arrived_range:
                if len(self.deviceschedules[uuid]) > 0:
                    del self.deviceschedules[uuid][0]

        if len(self.deviceschedules[uuid]) == 0:
            scheduled_points = []
            if no_overlap:
                for dev in self.get_active_devices():
                    if dev.get('no_overlap') and dev['fetching'] == 'walk_pokestop':
                        if dev['deviceid'] in self.devicesscheduling:
                            d = {}
                            d['latitude'] = deviceworker['latitude']
                            d['longitude'] = deviceworker['longitude']
                            return jsonify(d)

                        scheduled_points += [item[2] for item in self.deviceschedules[dev['deviceid']]]

            self.devicesscheduling.append(uuid)

            if not self.geofences:
                from .geofence import Geofences
                self.geofences = Geofences()

            self.deviceschedules[uuid] = Pokestop.get_nearby_pokestops(latitude, longitude, maxradius, questless, maxpoints, geofence, scheduled_points, self.geofences)
            nextlatitude = latitude
            nextlongitude = longitude
            if questless and len(self.deviceschedules[uuid]) == 0:
                self.deviceschedules[uuid] = Pokestop.get_nearby_pokestops(latitude, longitude, maxradius, False, maxpoints, geofence, scheduled_points, self.geofences)
            if len(self.deviceschedules[uuid]) == 0:
                return self.scan_loc(mapcontrolled, uuid, latitude, longitude, request_json)
        else:
            nextlatitude = deviceworker['latitude']
            nextlongitude = deviceworker['longitude']

        nexttarget = self.deviceschedules[uuid][0]

        dlat = abs(nexttarget[0] - nextlatitude)
        dlong = abs(nexttarget[1] - nextlongitude)

        distance_m = geopy.distance.vincenty((nextlatitude, nextlongitude), (nexttarget[0], nexttarget[1])).meters
        num_seconds = distance_m / speed * 3.6  # 7.6 = 2x walk speed
        # log.info("{} - Distance to go: {} metres, Time until arrival: {} seconds".format(deviceworker['name'], distance_m, num_seconds))

        if num_seconds <= 1.0:
            nextlatitude = nexttarget[0]
            nextlongitude = nexttarget[1]
        else:
            dlat_per_second = dlat / num_seconds
            dlong_per_second = dlong / num_seconds

            if nextlatitude < nexttarget[0]:
                nextlatitude += dlat_per_second
            else:
                nextlatitude -= dlat_per_second

            if nextlongitude < nexttarget[1]:
                nextlongitude += dlong_per_second
            else:
                nextlongitude -= dlong_per_second

        deviceworker['latitude'] = round(nextlatitude, 5)
        deviceworker['longitude'] = round(nextlongitude, 5)
        deviceworker['last_updated'] = datetime.utcnow()
        deviceworker['fetching'] = "walk_pokestop"

        self.save_device(deviceworker)

        d = {}
        d['latitude'] = deviceworker['latitude']
        d['longitude'] = deviceworker['longitude']

        return jsonify(d)

    def teleport_gym(self, mapcontrolled, uuid, latitude, longitude, request_json):
        deviceworker = self.get_device(uuid, latitude, longitude)

        args = get_args()
        dt_now = datetime.utcnow()

        self.track_event('fetch', 'teleport_gym', uuid)

        if uuid not in self.deviceschedules:
            self.deviceschedules[uuid] = []

        if deviceworker['fetching'] == "jump_now":
            deviceworker['last_updated'] = dt_now
            deviceworker['fetching'] = "teleport_gym"

            self.save_device(deviceworker)

            d = {}
            d['latitude'] = deviceworker['latitude']
            d['longitude'] = deviceworker['longitude']

            return jsonify(d)

        if uuid in self.devicesscheduling:
            if len(self.deviceschedules[uuid]) == 0:
                d = {}
                d['latitude'] = deviceworker['latitude']
                d['longitude'] = deviceworker['longitude']

                return jsonify(d)
            else:
                self.devicesscheduling.remove(uuid)

        scheduletimeout = request_json.get('scheduletimeout', args.scheduletimeout)
        maxradius = request_json.get('maxradius', args.maxradius)
        teleport_interval = request_json.get('teleport_interval', args.teleport_interval)
        teleport_ignore = request_json.get('teleport_ignore', args.teleport_ignore)
        raidless = request_json.get('raidless', False)
        maxpoints = request_json.get('maxpoints', False)
        geofence = request_json.get('geofence', "")
        no_overlap = request_json.get('no_overlap', False)
        exraidonly = request_json.get('exraidonly', False)
        oldest_first = request_json.get('oldest_first', False)

        if not isinstance(scheduletimeout, (int, long)):
            try:
                scheduletimeout = int(scheduletimeout)
            except:
                pass
        if not isinstance(maxradius, (int, long)):
            try:
                maxradius = int(maxradius)
            except:
                pass
        if not isinstance(teleport_interval, (int, long)):
            try:
                teleport_interval = int(teleport_interval)
            except:
                pass
        if not isinstance(teleport_ignore, (int, long)):
            try:
                teleport_ignore = int(teleport_ignore)
            except:
                pass
        if not isinstance(raidless, (bool, int, long)):
            try:
                if raidless.lower() == 'true':
                    raidless = True
                elif raidless.lower() == 'false':
                    raidless = False
                else:
                    raidless = int(raidless)
            except:
                pass
        if not isinstance(maxpoints, (bool, int, long)):
            try:
                if maxpoints.lower() == 'true':
                    maxpoints = True
                elif maxpoints.lower() == 'false':
                    maxpoints = False
                else:
                    maxpoints = int(maxpoints)
            except:
                pass
        if not isinstance(no_overlap, bool):
            try:
                if no_overlap.lower() == 'true':
                    no_overlap = True
                else:
                    no_overlap = False
            except:
                pass
        if not isinstance(mapcontrolled, bool):
            try:
                if mapcontrolled.lower() == 'true':
                    mapcontrolled = True
                else:
                    mapcontrolled = False
            except:
                pass
        if not isinstance(exraidonly, bool):
            try:
                if exraidonly.lower() == 'true':
                    exraidonly = True
                else:
                    exraidonly = False
            except:
                pass

        deviceworker['no_overlap'] = no_overlap
        deviceworker['mapcontrolled'] = mapcontrolled

        last_updated = deviceworker['last_updated']
        difference = (dt_now - last_updated).total_seconds()
        if (deviceworker['fetching'] == 'IDLE' and difference > scheduletimeout * 60) or (deviceworker['fetching'] != 'IDLE' and deviceworker['fetching'] != "teleport_gym"):
            self.deviceschedules[uuid] = []

        ls_times = self.devices_last_scanned_times.get(uuid)
        last_teleport_time = self.devices_last_teleport_time[uuid] if uuid in self.devices_last_teleport_time else last_updated
        difference = (dt_now - last_teleport_time).total_seconds()

        waitForGymsOrPokestops = args.teleport_wait_gyms_or_pokestops
        waitForGyms = args.teleport_wait_gyms
        waitForPokestops = args.teleport_wait_pokestops
        waitForNearbyOrWildPokemon = args.teleport_wait_nearby_or_wild_pokemon
        waitForNearbyPokemon = args.teleport_wait_nearby_pokemon
        waitForWildPokemon = args.teleport_wait_wild_pokemon
        waitForTeleportInterval = args.teleport_wait_teleport_interval

        teleportToNextLocation = True

        if waitForGymsOrPokestops == True and ((ls_times is None) or (ls_times.get('forts') is None) or (ls_times.get('forts') < last_teleport_time)):
            teleportToNextLocation = False
        if waitForGyms == True and ((ls_times is None) or (ls_times.get('gyms') is None) or (ls_times.get('gyms') < last_teleport_time)):
            teleportToNextLocation = False
        if waitForPokestops == True and ((ls_times is None) or (ls_times.get('pokestops') is None) or (ls_times.get('pokestops') < last_teleport_time)):
            teleportToNextLocation = False
        if waitForNearbyOrWildPokemon == True and ((ls_times is None) or (ls_times.get('pokemon') is None) or (ls_times.get('pokemon') < last_teleport_time)):
            teleportToNextLocation = False
        if waitForNearbyPokemon == True and ((ls_times is None) or (ls_times.get('nearby_pokemon') is None) or (ls_times.get('nearby_pokemon') < last_teleport_time)):
            teleportToNextLocation = False
        if waitForWildPokemon == True and ((ls_times is None) or (ls_times.get('wild_pokemon') is None) or (ls_times.get('wild_pokemon') < last_teleport_time)):
            teleportToNextLocation = False
        if waitForTeleportInterval == True and (difference < teleport_interval):
            teleportToNextLocation = False

        if (difference >= args.teleport_wait_timeout):
            # timeout expired, force the teleport
            teleportToNextLocation = True

        if teleportToNextLocation == True:
            if len(self.deviceschedules[uuid]) > 0:
                del self.deviceschedules[uuid][0]
            self.devices_last_teleport_time[uuid] = dt_now
            self.save_device(deviceworker)

        if len(self.deviceschedules[uuid]) == 0:
            scheduled_points = []
            if no_overlap:
                for dev in self.get_active_devices():
                    if dev.get('no_overlap') and dev['fetching'] == 'teleport_gym':
                        if dev['deviceid'] in self.devicesscheduling:
                            d = {}
                            d['latitude'] = deviceworker['latitude']
                            d['longitude'] = deviceworker['longitude']
                            return jsonify(d)

                        scheduled_points += [item[2] for item in self.deviceschedules[dev['deviceid']]]

            self.devicesscheduling.append(uuid)

            if not self.geofences:
                from .geofence import Geofences
                self.geofences = Geofences()

            log.warning("Geofences: ".format(geofence))

            self.deviceschedules[uuid] = Gym.get_nearby_gyms(latitude, longitude, maxradius, teleport_ignore, raidless, maxpoints, geofence, scheduled_points, self.geofences, exraidonly, oldest_first)
            if raidless and len(self.deviceschedules[uuid]) == 0:
                self.deviceschedules[uuid] = Gym.get_nearby_gyms(latitude, longitude, maxradius, teleport_ignore, False, maxpoints, geofence, scheduled_points, self.geofences, exraidonly, oldest_first)
            if len(self.deviceschedules[uuid]) == 0:
                return self.scan_loc(mapcontrolled, uuid, latitude, longitude, request_json)

            self.devices_last_teleport_time[uuid] = dt_now
            self.save_device(deviceworker)

        nexttarget = self.deviceschedules[uuid][0]

        if args.jitter:
            jitter_nexttarget = jitter_location([nexttarget[0], nexttarget[1], 0])

            nextlatitude = jitter_nexttarget[0]
            nextlongitude = jitter_nexttarget[1]
        else:
            nextlatitude = nexttarget[0]
            nextlongitude = nexttarget[1]

        deviceworker['latitude'] = round(nextlatitude, 5)
        deviceworker['longitude'] = round(nextlongitude, 5)
        deviceworker['last_updated'] = dt_now
        deviceworker['fetching'] = "teleport_gym"
        self.save_device(deviceworker)

        d = {}
        d['latitude'] = deviceworker['latitude']
        d['longitude'] = deviceworker['longitude']

        return jsonify(d)

    def teleport_gpx(self, mapcontrolled, uuid, latitude, longitude, request_json):
        args = get_args()
        dt_now = datetime.utcnow()

        if latitude == 0 and longitude == 0:
            latitude = self.current_location[0]
            longitude = self.current_location[1]

        deviceworker = self.get_device(uuid, latitude, longitude)

        self.track_event('fetch', 'teleport_gpx', uuid)

        if uuid not in self.deviceschedules:
            self.deviceschedules[uuid] = []

        if deviceworker['fetching'] == "jump_now":
            deviceworker['last_updated'] = dt_now
            deviceworker['fetching'] = "teleport_gpx"

            self.save_device(deviceworker)

            d = {}
            d['latitude'] = deviceworker['latitude']
            d['longitude'] = deviceworker['longitude']

            return jsonify(d)

        if uuid in self.devicesscheduling:
            if len(self.deviceschedules[uuid]) == 0:
                d = {}
                d['latitude'] = deviceworker['latitude']
                d['longitude'] = deviceworker['longitude']

                return jsonify(d)
            else:
                self.devicesscheduling.remove(uuid)

        scheduletimeout = args.scheduletimeout
        teleport_interval = args.teleport_interval
        scheduletimeout = request_json.get('scheduletimeout', scheduletimeout)
        teleport_interval = request_json.get('teleport_interval', teleport_interval)

        if not isinstance(scheduletimeout, (int, long)):
            try:
                scheduletimeout = int(scheduletimeout)
            except:
                pass
        if not isinstance(teleport_interval, (int, long)):
            try:
                teleport_interval = int(teleport_interval)
            except:
                pass
        if not isinstance(mapcontrolled, bool):
            try:
                if mapcontrolled.lower() == 'true':
                    mapcontrolled = True
                else:
                    mapcontrolled = False
            except:
                pass

        deviceworker['no_overlap'] = False
        deviceworker['mapcontrolled'] = mapcontrolled

        last_updated = deviceworker['last_updated']
        difference = (dt_now - last_updated).total_seconds()
        if (deviceworker['fetching'] == 'IDLE' and difference > scheduletimeout * 60) or (deviceworker['fetching'] != 'IDLE' and deviceworker['fetching'] != "teleport_gpx"):
            self.deviceschedules[uuid] = []

        routename = ""
        routename = request_json.get('route', "", type=str)
        if routename is None or routename == "":
            routename = uuid

        if (deviceworker['fetching'] == 'teleport_gpx' and deviceworker.get('route', '') != routename and deviceworker.get('route', '') != ''):
            self.deviceschedules[uuid] = []

        ls_times = self.devices_last_scanned_times.get(uuid)
        last_teleport_time = self.devices_last_teleport_time[uuid] if uuid in self.devices_last_teleport_time else last_updated
        difference = (dt_now - last_teleport_time).total_seconds()

        waitForGymsOrPokestops = args.teleport_wait_gyms_or_pokestops
        waitForGyms = args.teleport_wait_gyms
        waitForPokestops = args.teleport_wait_pokestops
        waitForNearbyOrWildPokemon = args.teleport_wait_nearby_or_wild_pokemon
        waitForNearbyPokemon = args.teleport_wait_nearby_pokemon
        waitForWildPokemon = args.teleport_wait_wild_pokemon
        waitForTeleportInterval = args.teleport_wait_teleport_interval

        teleportToNextLocation = True

        if waitForGymsOrPokestops == True and ((ls_times is None) or (ls_times.get('forts') is None) or (ls_times.get('forts') < last_teleport_time)):
            teleportToNextLocation = False
        if waitForGyms == True and ((ls_times is None) or (ls_times.get('gyms') is None) or (ls_times.get('gyms') < last_teleport_time)):
            teleportToNextLocation = False
        if waitForPokestops == True and ((ls_times is None) or (ls_times.get('pokestops') is None) or (ls_times.get('pokestops') < last_teleport_time)):
            teleportToNextLocation = False
        if waitForNearbyOrWildPokemon == True and ((ls_times is None) or (ls_times.get('pokemon') is None) or (ls_times.get('pokemon') < last_teleport_time)):
            teleportToNextLocation = False
        if waitForNearbyPokemon == True and ((ls_times is None) or (ls_times.get('nearby_pokemon') is None) or (ls_times.get('nearby_pokemon') < last_teleport_time)):
            teleportToNextLocation = False
        if waitForWildPokemon == True and ((ls_times is None) or (ls_times.get('wild_pokemon') is None) or (ls_times.get('wild_pokemon') < last_teleport_time)):
            teleportToNextLocation = False
        if waitForTeleportInterval == True and (difference < teleport_interval):
            teleportToNextLocation = False

        if (difference >= args.teleport_wait_timeout):
            # timeout expired, force the teleport
            teleportToNextLocation = True

        if teleportToNextLocation == True:
            if len(self.deviceschedules[uuid]) > 0:
                del self.deviceschedules[uuid][0]
            self.devices_last_teleport_time[uuid] = dt_now
            self.save_device(deviceworker)

        if len(self.deviceschedules[uuid]) == 0:
            gpxfilename = ""
            if routename != "":
                gpxfilename = os.path.join(
                    args.root_path,
                    'gpx',
                    routename + ".gpx")
            if gpxfilename == "" or not os.path.isfile(gpxfilename):
                log.warning("No or incorrect GPX supplied: {}".format(gpxfilename))
                return self.scan_loc(mapcontrolled, uuid, latitude, longitude, request_json)

            self.devicesscheduling.append(uuid)
            self.deviceschedules[uuid] = self.get_gpx_route(gpxfilename)
            if len(self.deviceschedules[uuid]) == 0:
                return self.scan_loc(mapcontrolled, uuid, latitude, longitude, request_json)
            self.devices_last_teleport_time[uuid] = dt_now
            deviceworker['route'] = routename
            self.save_device(deviceworker)

        nexttarget = self.deviceschedules[uuid][0]

        if args.jitter:
            jitter_nexttarget = jitter_location([nexttarget[0], nexttarget[1], 0])

            nextlatitude = jitter_nexttarget[0]
            nextlongitude = jitter_nexttarget[1]
        else:
            nextlatitude = nexttarget[0]
            nextlongitude = nexttarget[1]

        deviceworker['latitude'] = round(nextlatitude, 5)
        deviceworker['longitude'] = round(nextlongitude, 5)
        deviceworker['last_updated'] = dt_now
        deviceworker['fetching'] = "teleport_gpx"
        self.save_device(deviceworker)

        d = {}
        d['latitude'] = deviceworker['latitude']
        d['longitude'] = deviceworker['longitude']

        return jsonify(d)

    def old_teleport_gpx(self):
        return self.unifiedEndpoints('teleport_gpx')

    def old_walk_pokestop(self):
        return self.unifiedEndpoints('walk_pokestop')

    def old_walk_gpx(self):
        return self.unifiedEndpoints('walk_gpx')

    def old_walk_spawnpoint(self):
        return self.unifiedEndpoints('walk_spawnpoint')

    def old_mapcontrolled(self):
        return self.unifiedEndpoints('mapcontrolled')

    def old_teleport_gym(self):
        return self.unifiedEndpoints('teleport_gym')

    def old_scan_loc(self):
        return self.unifiedEndpoints('scan_loc')

    def unifiedEndpoints(self, endpoint):
        if request.method == "GET":
            request_json = request.args
        else:
            request_json = request.get_json()

        map_lat = self.current_location[0]
        map_lng = self.current_location[1]

        uuid = request_json.get('uuid', '')
        if uuid == "":
            d = {}
            d['latitude'] = map_lat
            d['longitude'] = map_lng

            return jsonify(d)

        canusedevice, devicename = self.trusted_device(uuid)
        if not canusedevice:
            d = {}
            d['latitude'] = map_lat
            d['longitude'] = map_lng

            return jsonify(d)

        lat = float(request_json.get('latitude', request_json.get('latitude:', 0)))
        lng = float(request_json.get('longitude', request_json.get('longitude:', 0)))

        latitude = round(lat, 5)
        longitude = round(lng, 5)

        if latitude == 0 and longitude == 0:
            latitude = map_lat
            longitude = map_lng

        deviceworker = self.get_device(uuid, latitude, longitude)

        # Send coordinates where to start if device never have scanned before
        if not deviceworker['last_scanned']:
            d = {}
            d['latitude'] = map_lat
            d['longitude'] = map_lng

            return jsonify(d)

        # Update the username of the device is sent along and incorrect in database
        username = request_json.get('username', '')
        if username != "" and username != deviceworker['username']:
            log.info('Device {} updating ownerusername: {} => {}'.format(uuid, deviceworker['username'], username))
            deviceworker['username'] = username
            self.save_device(deviceworker, True)
        # Update deviceusername
        if devicename != "" and devicename != deviceworker['name']:
            log.info('Device {} updating deviceusername: {} => {}'.format(uuid, deviceworker['name'], devicename))
            deviceworker['name'] = devicename
            self.save_device(deviceworker, True)
# Update deviceusername
        requestedEndpoint = re.sub(r'((\?|\&)longitude=-?[0-9]*\.?[0-9]*)|((\?|\&)latitude=-?[0-9]*\.?[0-9]*)|((\?|\&)timestamp=[0-9]*\.[0-9]*)|((\?|\&)uuid=[A-F0-9-]{36})','',request.full_path)
        if requestedEndpoint != "" and requestedEndpoint != deviceworker['requestedEndpoint']:
            log.info('Device {} updating requestedEndpoint: {} => {}'.format(uuid, deviceworker['requestedEndpoint'], requestedEndpoint))
            deviceworker['requestedEndpoint'] = requestedEndpoint
            self.save_device(deviceworker, True)
# Update PoGo version
        pogoversion = re.sub(r'pokemongo/([^\ ]*).*', r'\1', request.headers.get('User-Agent','Unknown'))
        if pogoversion != "" and pogoversion != deviceworker['pogoversion']:
            log.info('Device {} updating pogoversion: {} => {}'.format(uuid, deviceworker['pogoversion'], pogoversion))
            deviceworker['pogoversion'] = pogoversion
            self.save_device(deviceworker, True)
#MapControlled section
        if (endpoint.lower() == "mapcontrolled"):
            endpoint_re = "(http[s]?://[^/]*/|/|)(?P<endpoint>[^\?]*)\??(?P<attributes>.*)"
            endpointMC = str(deviceworker.get('endpoint', ''))
            endpointMC_base = re.sub(endpoint_re, '\g<endpoint>',  endpointMC)
            endpointMC_attribArray = re.split('&', re.sub(endpoint_re, '\g<attributes>',  endpointMC))
            endpointMC_attribMD = MultiDict()
            for pair in endpointMC_attribArray:
                endpointMC_attribMD.add(re.sub("=.*","", pair),re.sub(".*=",'',pair))
            if (endpointMC_base == "" or endpointMC.lower() == "scan_loc"):
                return self.scan_loc(True, uuid, latitude, longitude, endpointMC_attribMD)
            elif (endpointMC_base == "teleport_gym"):
                return self.teleport_gym(True, uuid, latitude, longitude, endpointMC_attribMD)
            elif (endpointMC_base.lower() == "teleport_gpx"):
                return self.teleport_gpx(True, uuid, latitude, longitude, endpointMC_attribMD)
            elif (endpointMC_base.lower() == "walk_pokestop"):
                return self.walk_pokestop(True, uuid, latitude, longitude, endpointMC_attribMD)
            elif (endpointMC_base.lower() == "walk_gpx"):
                return self.walk_gpx(True, uuid, latitude, longitude, endpointMC_attribMD)
            elif (endpointMC_base.lower() == "walk_spawnpoint"):
                return self.walk_spawnpoint(True, uuid, latitude, longitude, endpointMC_attribMD)
            return "Endpoint {} (mapcontrolled) not converted ".format(endpoint)

#Device requiested endpoints
        elif (endpoint.lower() == "teleport_gym"):
            return self.teleport_gym(False, uuid, latitude, longitude, request_json)
        elif (endpoint.lower() == "scan_loc"):
            return self.scan_loc(False, uuid, latitude, longitude, request_json)
        elif (endpoint.lower() == "teleport_gpx"):
            return self.teleport_gpx(False, uuid, latitude, longitude, request_json)
        elif (endpoint.lower() == "walk_pokestop"):
            return self.walk_pokestop(False, uuid, latitude, longitude, request_json)
        elif (endpoint.lower() == "walk_gpx"):
            return self.walk_gpx(False, uuid, latitude, longitude, request_json)
        elif (endpoint.lower() == "walk_spawnpoint"):
            return self.walk_spawnpoint(False, uuid, latitude, longitude, request_json)
        elif (endpoint.lower() == "dummy"):
            return "Dummmy :D"
        return "Endpoint {} not converted.".format(endpoint)

    def scan_loc(self, mapcontrolled, uuid, latitude, longitude, request_json):
        args = get_args()
        deviceworker = self.get_device(uuid, latitude, longitude)

        if deviceworker['fetching'] == "jump_now":
            deviceworker['last_updated'] = datetime.utcnow()
            deviceworker['fetching'] = "scan_loc"
            self.save_device(deviceworker)

            d = {}
            d['latitude'] = deviceworker['latitude']
            d['longitude'] = deviceworker['longitude']

            return jsonify(d)

        deviceworker['no_overlap'] = False
        deviceworker['mapcontrolled'] = mapcontrolled

        currentlatitude = round(deviceworker['latitude'], 5)
        currentlongitude = round(deviceworker['longitude'], 5)
        centerlatitude = round(deviceworker['centerlatitude'], 5)
        centerlongitude = round(deviceworker['centerlongitude'], 5)
        radius = deviceworker['radius']
        step = deviceworker['step']
        direction = deviceworker['direction']

        maxradius = request_json.get('maxradius', args.maxradius)
        stepsize = request_json.get('stepsize', args.stepsize)
        teleport_factor = request_json.get('teleport_factor', args.teleport_factor)

        if latitude != 0 and longitude != 0 and (abs(latitude - currentlatitude) > (radius + teleport_factor) * stepsize or abs(longitude - currentlongitude) > (radius + teleport_factor) * stepsize):
            centerlatitude = latitude
            centerlongitude = longitude
            radius = 0
            step = 0
            direction = "U"

        if (abs(centerlatitude - currentlatitude) > (radius + teleport_factor) * stepsize or abs(centerlongitude - currentlongitude) > (radius + teleport_factor) * stepsize):
            centerlatitude = latitude
            centerlongitude = longitude
            radius = 0
            step = 0
            direction = "U"

        step += 1

        if radius == 0:
            radius += 1
        elif direction == "U":
            currentlatitude += stepsize
            if currentlatitude > centerlatitude + radius * stepsize:
                currentlatitude -= stepsize
                direction = "R"
                currentlongitude += stepsize
                if abs(currentlongitude - centerlongitude) < stepsize:
                    direction = "U"
                    currentlatitude += stepsize
                    radius += 1
                    step = 0
        elif direction == "R":
            currentlongitude += stepsize
            if currentlongitude > centerlongitude + radius * stepsize:
                currentlongitude -= stepsize
                direction = "D"
                currentlatitude -= stepsize
            elif abs(currentlongitude - centerlongitude) < stepsize:
                direction = "U"
                currentlatitude += stepsize
                radius += 1
                step = 0
        elif direction == "D":
            currentlatitude -= stepsize
            if currentlatitude < centerlatitude - radius * stepsize:
                currentlatitude += stepsize
                direction = "L"
                currentlongitude -= stepsize
        elif direction == "L":
            currentlongitude -= stepsize
            if currentlongitude < centerlongitude - radius * stepsize:
                currentlongitude += stepsize
                direction = "U"
                currentlatitude += stepsize

        if maxradius > 0 and geopy.distance.vincenty((currentlatitude, currentlongitude), (centerlatitude, centerlongitude)).km > maxradius:
            currentlatitude = centerlatitude
            currentlongitude = centerlongitude
            radius = 0
            step = 0
            direction = "U"

        deviceworker['latitude'] = round(currentlatitude, 5)
        deviceworker['longitude'] = round(currentlongitude, 5)
        deviceworker['centerlatitude'] = round(centerlatitude, 5)
        deviceworker['centerlongitude'] = round(centerlongitude, 5)
        deviceworker['radius'] = radius
        deviceworker['step'] = step
        deviceworker['direction'] = direction
        deviceworker['last_updated'] = datetime.utcnow()
        deviceworker['fetching'] = "scan_loc"

        self.save_device(deviceworker)

        # log.info(request)

        d = {}
        d['latitude'] = deviceworker['latitude']
        d['longitude'] = deviceworker['longitude']

        return jsonify(d)

    def next_loc(self):
        lat = None
        lon = None
        # Part of query string.
        if request.args:
            lat = request.args.get('lat', type=float)
            lon = request.args.get('lon', type=float)
            coords = request.args.get('coords', type=str)
            uuid = request.args.get('uuid', type=str)
        # From post requests.
        if request.form:
            lat = request.form.get('lat', type=float)
            lon = request.form.get('lon', type=float)
            coords = request.form.get('coords', type=str)
            uuid = request.form.get('uuid', type=str)

        if not ((lat and lon) or coords):
            log.warning('Invalid next location: (%s,%s) or (%s)', lat, lon, coords)
            return 'bad parameters', 400
        else:
            if not (lat and lon):
                coordslist = coords.split(',')
                lat = float(coordslist[0])
                lon = float(coordslist[1])
            if uuid:
                return self.changeDeviceLoc(lat, lon, uuid)
            self.location_queue.put((lat, lon, 0))
            self.set_current_location((lat, lon, 0))
            log.info('Changing next location: %s,%s', lat, lon)
            return self.loc()

    def new_name(self):
        name = None
        uuid = None
        # Part of query string.
        if request.args:
            name = request.args.get('name', type=str)
            uuid = request.args.get('uuid', type=str)
        # From post requests.
        if request.form:
            name = request.form.get('name', type=str)
            uuid = request.form.get('uuid', type=str)

        if not (name and uuid):
            log.warning('Missing name: %s or uuid: %s', name, uuid)
            return 'bad parameters', 400
        else:
            map_lat = self.current_location[0]
            map_lng = self.current_location[1]

            deviceworker = self.get_device(uuid, map_lat, map_lng)
            deviceworker['name'] = name

            return self.save_device(deviceworker, True)

    def new_username(self):
        username = None
        uuid = None
        # Part of query string.
        if request.args:
            username = request.args.get('username', type=str)
            uuid = request.args.get('uuid', type=str)
        # From post requests.
        if request.form:
            username = request.form.get('username', type=str)
            uuid = request.form.get('uuid', type=str)

        if not (username and uuid):
            log.warning('Missing username: %s or uuid: %s', username, uuid)
            return 'bad parameters', 400
        else:
            map_lat = self.current_location[0]
            map_lng = self.current_location[1]

            deviceworker = self.get_device(uuid, map_lat, map_lng)
            deviceworker['username'] = username

            return self.save_device(deviceworker, True)

    def new_endpoint(self):
        endpoint = None
        uuid = None
        # Part of query string.
        if request.args:
            endpoint = request.args.get('endpoint', type=str)
            uuid = request.args.get('uuid', type=str)
        # From post requests.
        if request.form:
            endpoint = request.form.get('endpoint', type=str)
            uuid = request.form.get('uuid', type=str)

        if not (endpoint and uuid):
            log.warning('Missing endpoint: %s or uuid: %s', endpoint, uuid)
            return 'bad parameters', 400
        else:
            map_lat = self.current_location[0]
            map_lng = self.current_location[1]

            endpoint = endpoint.replace('||', '?')
            endpoint = endpoint.replace('|', '&')

            deviceworker = self.get_device(uuid, map_lat, map_lng)
            log.info("Device {} change endpoint: {} => {}".format(uuid, deviceworker['endpoint'], endpoint))
            deviceworker['endpoint'] = endpoint

            return self.save_device(deviceworker, True)

    def list_pokemon(self):
        # todo: Check if client is Android/iOS/Desktop for geolink, currently
        # only supports Android.
        pokemon_list = []

        # Allow client to specify location.
        lat = request.args.get('lat', self.current_location[0], type=float)
        lon = request.args.get('lon', self.current_location[1], type=float)
        origin_point = LatLng.from_degrees(lat, lon)

        for pokemon in convert_pokemon_list(
                Pokemon.get_active(None, None, None, None)):
            pokemon_point = LatLng.from_degrees(pokemon['latitude'],
                                                pokemon['longitude'])
            diff = pokemon_point - origin_point
            diff_lat = diff.lat().degrees
            diff_lng = diff.lng().degrees
            direction = (('N' if diff_lat >= 0 else 'S')
                         if abs(diff_lat) > 1e-4 else '') +\
                        (('E' if diff_lng >= 0 else 'W')
                         if abs(diff_lng) > 1e-4 else '')
            entry = {
                'id': pokemon['pokemon_id'],
                'name': pokemon['pokemon_name'],
                'card_dir': direction,
                'distance': int(origin_point.get_distance(
                    pokemon_point).radians * 6366468.241830914),
                'time_to_disappear': '%d min %d sec' % (divmod(
                    (pokemon['disappear_time'] - datetime.utcnow()).seconds,
                    60)),
                'disappear_time': pokemon['disappear_time'],
                'disappear_sec': (
                    pokemon['disappear_time'] - datetime.utcnow()).seconds,
                'latitude': pokemon['latitude'],
                'longitude': pokemon['longitude']
            }
            pokemon_list.append((entry, entry['distance']))
        pokemon_list = [y[0] for y in sorted(pokemon_list, key=lambda x: x[1])]
        args = get_args()
        visibility_flags = {
            'custom_css': args.custom_css,
            'custom_js': args.custom_js
        }

        return render_template('mobile_list.html',
                               pokemon_list=pokemon_list,
                               origin_lat=lat,
                               origin_lng=lon,
                               show=visibility_flags
                               )

    def get_stats(self):
        args = get_args()
        visibility_flags = {
            'custom_css': args.custom_css,
            'custom_js': args.custom_js
        }

        return render_template('statistics.html',
                               lat=self.current_location[0],
                               lng=self.current_location[1],
                               gmaps_key=args.gmaps_key,
                               show=visibility_flags,
                               mapname=args.mapname,
                               analyticskey=str(args.google_analytics_key),
                               usegoogleanalytics=True if str(args.google_analytics_key) != '' else False,
                               )

    def get_gymdata(self):
        gym_id = request.args.get('id')
        gym = Gym.get_gym(gym_id)

        return jsonify(gym)

    def get_pokestopdata(self):
        pokestop_id = request.args.get('id')
        pokestop = Pokestop.get_stop(pokestop_id)

        return jsonify(pokestop)

    def get_deviceworkerdata(self):
        deviceworker_id = request.args.get('id')
        deviceworker = DeviceWorker.get_active_by_id(deviceworker_id)

        return jsonify(deviceworker)


class CustomJSONEncoder(JSONEncoder):

    def default(self, obj):
        try:
            if isinstance(obj, datetime):
                if obj.utcoffset() is not None:
                    obj = obj - obj.utcoffset()
                millis = int(
                    calendar.timegm(obj.timetuple()) * 1000 +
                    obj.microsecond / 1000
                )
                return millis
            iterable = iter(obj)
        except TypeError:
            pass
        else:
            return list(iterable)
        return JSONEncoder.default(self, obj)
