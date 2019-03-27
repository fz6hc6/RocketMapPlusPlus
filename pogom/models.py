#!/usr/bin/python
# -*- coding: utf-8 -*-

import logging
import itertools
import calendar
import sys
import gc
import time
import math

from functools import reduce

from peewee import (InsertQuery, Check, CompositeKey, ForeignKeyField,
                    SmallIntegerField, IntegerField, CharField, DoubleField,
                    BooleanField, DateTimeField, fn, DeleteQuery, FloatField,
                    TextField, BigIntegerField, PrimaryKeyField,
                    JOIN, OperationalError, SQL)
from playhouse.flask_utils import FlaskDB
from playhouse.pool import PooledMySQLDatabase
from playhouse.shortcuts import RetryOperationalError, case
from playhouse.migrate import migrate, MySQLMigrator
import datetime
from cachetools import TTLCache
from cachetools import cached
from timeit import default_timer
import geopy
from collections import OrderedDict
from flask import json

from pogom.utils import (get_pokemon_name, get_pokemon_types,
                    get_args, cellid, in_radius, date_secs, clock_between,
                    get_move_name, get_move_damage, get_move_energy,
                    get_move_type, calc_pokemon_level, peewee_attr_to_col,
                    get_quest_icon, get_quest_quest_text, get_quest_reward_text,
                    get_timezone_offset, point_is_scheduled)
from pogom.transform import transform_from_wgs_to_gcj, get_new_coords
from pogom.customLog import printPokemon

from pogom.account import check_login, setup_api, pokestop_spinnable, spin_pokestop
from pogom.proxy import get_new_proxy
from pogom.apiRequests import encounter

from pogom.protos.pogoprotos.map.weather.gameplay_weather_pb2 import *
from pogom.protos.pogoprotos.map.weather.weather_alert_pb2 import *

log = logging.getLogger(__name__)

args = get_args()
flaskDb = FlaskDB()
cache = TTLCache(maxsize=100, ttl=60 * 5)

db_schema_version = 62


class MyRetryDB(RetryOperationalError, PooledMySQLDatabase):
    pass


# Reduction of CharField to fit max length inside 767 bytes for utf8mb4 charset
class Utf8mb4CharField(CharField):
    def __init__(self, max_length=191, *args, **kwargs):
        self.max_length = max_length
        super(CharField, self).__init__(*args, **kwargs)


class UBigIntegerField(BigIntegerField):
    db_field = 'bigint unsigned'


def init_database(app):
    log.info('Connecting to MySQL database on %s:%i...',
             args.db_host, args.db_port)
    db = MyRetryDB(
        args.db_name,
        user=args.db_user,
        password=args.db_pass,
        host=args.db_host,
        port=args.db_port,
        stale_timeout=30,
        max_connections=None,
        charset='utf8mb4')

    # Using internal method as the other way would be using internal var, we
    # could use initializer but db is initialized later
    flaskDb._load_database(app, db)
    if app is not None:
        flaskDb._register_handlers(app)
    return db


class BaseModel(flaskDb.Model):

    @classmethod
    def database(cls):
        return cls._meta.database

    @classmethod
    def get_all(cls):
        return [m for m in cls.select().dicts()]


class LatLongModel(BaseModel):

    @classmethod
    def get_all(cls):
        results = [m for m in cls.select().dicts()]
        if args.china:
            for result in results:
                result['latitude'], result['longitude'] = \
                    transform_from_wgs_to_gcj(
                        result['latitude'], result['longitude'])
        return results


class Pokemon(LatLongModel):
    # We are base64 encoding the ids delivered by the api
    # because they are too big for sqlite to handle.
    encounter_id = UBigIntegerField(primary_key=True)
    spawnpoint_id = Utf8mb4CharField(index=True, max_length=100)
    pokemon_id = SmallIntegerField(index=True)
    latitude = DoubleField()
    longitude = DoubleField()
    disappear_time = DateTimeField()
    individual_attack = SmallIntegerField(null=True)
    individual_defense = SmallIntegerField(null=True)
    individual_stamina = SmallIntegerField(null=True)
    move_1 = SmallIntegerField(null=True)
    move_2 = SmallIntegerField(null=True)
    cp = SmallIntegerField(null=True)
    cp_multiplier = FloatField(null=True)
    weight = FloatField(null=True)
    height = FloatField(null=True)
    gender = SmallIntegerField(null=True)
    costume = SmallIntegerField(null=True)
    form = SmallIntegerField(null=True)
    weather_boosted_condition = SmallIntegerField(null=True)
    last_modified = DateTimeField(
        null=True, index=True, default=datetime.datetime.utcnow)

    class Meta:
        indexes = (
            (('latitude', 'longitude'), False),
            (('disappear_time', 'pokemon_id'), False)
        )

    @staticmethod
    def get_active(swLat, swLng, neLat, neLng, timestamp=0, oSwLat=None,
                   oSwLng=None, oNeLat=None, oNeLng=None, exclude=None):
        now_date = datetime.datetime.utcnow()
        query = Pokemon.select()

        if exclude:
            query = query.where(Pokemon.pokemon_id.not_in(list(exclude)))

        if not (swLat and swLng and neLat and neLng):
            query = (query
                     .where(Pokemon.disappear_time > now_date)
                     .dicts())
        elif timestamp > 0:
            # If timestamp is known only load modified Pokemon.
            query = (query
                     .where(((Pokemon.last_modified >
                              datetime.datetime.utcfromtimestamp(timestamp / 1000)) &
                             (Pokemon.disappear_time > now_date)) &
                            ((Pokemon.latitude >= swLat) &
                             (Pokemon.longitude >= swLng) &
                             (Pokemon.latitude <= neLat) &
                             (Pokemon.longitude <= neLng)))
                     .dicts())
        elif oSwLat and oSwLng and oNeLat and oNeLng:
            # Send Pokemon in view but exclude those within old boundaries.
            # Only send newly uncovered Pokemon.
            query = (query
                     .where(((Pokemon.disappear_time > now_date) &
                             (((Pokemon.latitude >= swLat) &
                               (Pokemon.longitude >= swLng) &
                               (Pokemon.latitude <= neLat) &
                               (Pokemon.longitude <= neLng))) &
                             ~((Pokemon.disappear_time > now_date) &
                               (Pokemon.latitude >= oSwLat) &
                               (Pokemon.longitude >= oSwLng) &
                               (Pokemon.latitude <= oNeLat) &
                               (Pokemon.longitude <= oNeLng))))
                     .dicts())
        else:
            query = (query
                     # Add 1 hour buffer to include spawnpoints that persist
                     # after tth, like shsh.
                     .where((Pokemon.disappear_time > now_date) &
                            (((Pokemon.latitude >= swLat) &
                              (Pokemon.longitude >= swLng) &
                              (Pokemon.latitude <= neLat) &
                              (Pokemon.longitude <= neLng))))
                     .dicts())
        return list(query)

    @staticmethod
    def get_active_by_id(ids, swLat, swLng, neLat, neLng):
        if not (swLat and swLng and neLat and neLng):
            query = (Pokemon
                     .select()
                     .where((Pokemon.pokemon_id << ids) &
                            (Pokemon.disappear_time > datetime.datetime.utcnow()))
                     .dicts())
        else:
            query = (Pokemon
                     .select()
                     .where((Pokemon.pokemon_id << ids) &
                            (Pokemon.disappear_time > datetime.datetime.utcnow()) &
                            (Pokemon.latitude >= swLat) &
                            (Pokemon.longitude >= swLng) &
                            (Pokemon.latitude <= neLat) &
                            (Pokemon.longitude <= neLng))
                     .dicts())

        return list(query)

    # Get all Pokémon spawn counts based on the last x hours.
    # More efficient than get_seen(): we don't do any unnecessary mojo.
    # Returns a dict:
    #   { 'pokemon': [ {'pokemon_id': '', 'count': 1} ], 'total': 1 }.
    @staticmethod
    def get_spawn_counts(hours):
        query = (Pokemon
                 .select(Pokemon.pokemon_id,
                         fn.Count(Pokemon.pokemon_id).alias('count')))

        # Allow 0 to query everything.
        if hours:
            hours = datetime.datetime.utcnow() - datetime.timedelta(hours=hours)
            # Not using WHERE speeds up the query.
            query = query.where(Pokemon.disappear_time > hours)

        query = query.group_by(Pokemon.pokemon_id).dicts()

        # We need a total count. Use reduce() instead of sum() for O(n)
        # instead of O(2n) caused by list comprehension.
        total = reduce(lambda x, y: x + y['count'], query, 0)

        return {'pokemon': query, 'total': total}

    @staticmethod
    @cached(cache)
    def get_seen(timediff):
        if timediff:
            timediff = datetime.datetime.utcnow() - datetime.timedelta(hours=timediff)

        # Note: pokemon_id+0 forces SQL to ignore the pokemon_id index
        # and should use the disappear_time index and hopefully
        # improve performance
        pokemon_count_query = (Pokemon
                               .select((Pokemon.pokemon_id+0).alias(
                                           'pokemon_id'),
                                       fn.COUNT((Pokemon.pokemon_id+0)).alias(
                                           'count'),
                                       fn.MAX(Pokemon.disappear_time).alias(
                                           'lastappeared')
                                       )
                               .where(Pokemon.disappear_time > timediff)
                               .group_by((Pokemon.pokemon_id+0))
                               .alias('counttable')
                               )
        query = (Pokemon
                 .select(Pokemon.pokemon_id,
                         Pokemon.disappear_time,
                         Pokemon.latitude,
                         Pokemon.longitude,
                         pokemon_count_query.c.count)
                 .join(pokemon_count_query,
                       on=(Pokemon.pokemon_id ==
                           pokemon_count_query.c.pokemon_id))
                 .distinct()
                 .where(Pokemon.disappear_time ==
                        pokemon_count_query.c.lastappeared)
                 .dicts()
                 )

        # Performance:  disable the garbage collector prior to creating a
        # (potentially) large dict with append().
        gc.disable()

        pokemon = []
        total = 0
        for p in query:
            p['pokemon_name'] = get_pokemon_name(p['pokemon_id'])
            pokemon.append(p)
            total += p['count']

        # Re-enable the GC.
        gc.enable()

        return {'pokemon': pokemon, 'total': total}

    @staticmethod
    def get_appearances(pokemon_id, timediff):
        '''
        :param pokemon_id: id of Pokemon that we need appearances for
        :param timediff: limiting period of the selection
        :return: list of Pokemon appearances over a selected period
        '''
        if timediff:
            timediff = datetime.datetime.utcnow() - datetime.timedelta(hours=timediff)
        query = (Pokemon
                 .select(Pokemon.latitude, Pokemon.longitude,
                         Pokemon.pokemon_id,
                         fn.Count(Pokemon.spawnpoint_id).alias('count'),
                         Pokemon.spawnpoint_id)
                 .where((Pokemon.pokemon_id == pokemon_id) &
                        (Pokemon.disappear_time > timediff)
                        )
                 .group_by(Pokemon.latitude, Pokemon.longitude,
                           Pokemon.pokemon_id, Pokemon.spawnpoint_id)
                 .dicts()
                 )

        if args.china:
            for result in query:
                result['latitude'], result['longitude'] = \
                    transform_from_wgs_to_gcj(
                        result['latitude'], result['longitude'])

        return list(query)

    @staticmethod
    def get_appearances_times_by_spawnpoint(pokemon_id, spawnpoint_id,
                                            timediff):

        '''
        :param pokemon_id: id of Pokemon that we need appearances times for.
        :param spawnpoint_id: spawnpoint id we need appearances times for.
        :param timediff: limiting period of the selection.
        :return: list of time appearances over a selected period.
        '''
        if timediff:
            timediff = datetime.datetime.utcnow() - datetime.timedelta(hours=timediff)
        query = (Pokemon
                 .select(Pokemon.disappear_time)
                 .where((Pokemon.pokemon_id == pokemon_id) &
                        (Pokemon.spawnpoint_id == spawnpoint_id) &
                        (Pokemon.disappear_time > timediff)
                        )
                 .order_by(Pokemon.disappear_time.asc())
                 .tuples()
                 )

        return list(itertools.chain(*query))


class Quest(BaseModel):
    pokestop_id = Utf8mb4CharField(primary_key=True, max_length=50)
    quest_type = Utf8mb4CharField(max_length=50)
    goal = IntegerField(null=True)
    reward_type = Utf8mb4CharField(max_length=50)
    reward_item = Utf8mb4CharField(max_length=50, null=True)
    reward_amount = IntegerField(null=True)
    quest_json = TextField(null=True)
    last_scanned = DateTimeField(default=datetime.datetime.utcnow, index=True)
    expiration = DateTimeField(null=True, index=True)

    @staticmethod
    def get_quests(swLat, swLng, neLat, neLng, timestamp=0, oSwLat=None,
                   oSwLng=None, oNeLat=None, oNeLng=None, lured=False):

        query = (Quest
                 .select(Quest.pokestop_id,
                         PokestopDetails.name,
                         PokestopDetails.url,
                         Pokestop.latitude,
                         Pokestop.longitude,
                         Quest.quest_type,
                         Quest.reward_type,
                         Quest.reward_item,
                         Quest.reward_amount,
                         Quest.last_scanned,
                         Quest.quest_json,
                         Quest.expiration)
                 .join(Pokestop, JOIN.LEFT_OUTER,
                       on=(Quest.pokestop_id == Pokestop.pokestop_id))
                 .join(PokestopDetails, JOIN.LEFT_OUTER,
                       on=(Quest.pokestop_id == PokestopDetails.pokestop_id)))

        if not (swLat and swLng and neLat and neLng):
            query = (query
                     .where(Quest.expiration >= datetime.datetime.utcnow())
                     .dicts())
        else:
            query = (query
                     .where((Quest.expiration >= datetime.datetime.utcnow()) &
                            (Pokestop.latitude >= swLat) &
                            (Pokestop.longitude >= swLng) &
                            (Pokestop.latitude <= neLat) &
                            (Pokestop.longitude <= neLng))
                     .dicts())

        for q in query:
            if q['quest_json'] is not None:
                q['quest_json'] = json.loads(q['quest_json'])
            q['icon'] = get_quest_icon(q['reward_type'], q['reward_item'])
            q['quest_text'] = get_quest_quest_text(q['quest_json'])
            q['reward_text'] = get_quest_reward_text(q['quest_json'])
            q['url'] = q.get('url', '').replace('http://', 'https://')
#        quests = {}
#        for quest in query:
#            if args.china:
#                quest['latitude'], quest['longitude'] = \
#                    transform_from_wgs_to_gcj(quest['latitude'], quest['longitude'])
#            quests[quest['pokestop_id']] = quest

        return query


class Pokestop(LatLongModel):
    pokestop_id = Utf8mb4CharField(primary_key=True, max_length=50)
    enabled = BooleanField()
    latitude = DoubleField()
    longitude = DoubleField()
    last_modified = DateTimeField(index=True)
    lure_expiration = DateTimeField(null=True, index=True)
    active_fort_modifier = Utf8mb4CharField(max_length=50)
    active_pokemon_id = SmallIntegerField(null=True)
    active_pokemon_expiration = DateTimeField(null=True, index=True)
    last_updated = DateTimeField(
        null=True, index=True, default=datetime.datetime.utcnow)

    class Meta:
        indexes = ((('latitude', 'longitude'), False),)

    @staticmethod
    def get_stops(swLat, swLng, neLat, neLng, timestamp=0, oSwLat=None,
                  oSwLng=None, oNeLat=None, oNeLng=None, lured=False):

        query = Pokestop.select(Pokestop.active_fort_modifier,
                                Pokestop.enabled, Pokestop.latitude,
                                Pokestop.longitude, Pokestop.last_modified,
                                Pokestop.lure_expiration, Pokestop.pokestop_id,
                                Pokestop.active_pokemon_id, Pokestop.active_pokemon_expiration,
                                Pokestop.last_updated)

        if not (swLat and swLng and neLat and neLng):
            query = (query
                     .dicts())
        elif timestamp > 0:
            query = (query
                     .where(((Pokestop.last_updated >
                              datetime.datetime.utcfromtimestamp(timestamp / 1000))) &
                            (Pokestop.latitude >= swLat) &
                            (Pokestop.longitude >= swLng) &
                            (Pokestop.latitude <= neLat) &
                            (Pokestop.longitude <= neLng))
                     .dicts())
        elif oSwLat and oSwLng and oNeLat and oNeLng and lured:
            query = (query
                     .where((((Pokestop.latitude >= swLat) &
                              (Pokestop.longitude >= swLng) &
                              (Pokestop.latitude <= neLat) &
                              (Pokestop.longitude <= neLng)) &
                             (Pokestop.active_fort_modifier.is_null(False))) &
                            ~((Pokestop.latitude >= oSwLat) &
                              (Pokestop.longitude >= oSwLng) &
                              (Pokestop.latitude <= oNeLat) &
                              (Pokestop.longitude <= oNeLng)) &
                             (Pokestop.active_fort_modifier.is_null(False)))
                     .dicts())
        elif oSwLat and oSwLng and oNeLat and oNeLng:
            # Send stops in view but exclude those within old boundaries. Only
            # send newly uncovered stops.
            query = (query
                     .where(((Pokestop.latitude >= swLat) &
                             (Pokestop.longitude >= swLng) &
                             (Pokestop.latitude <= neLat) &
                             (Pokestop.longitude <= neLng)) &
                            ~((Pokestop.latitude >= oSwLat) &
                              (Pokestop.longitude >= oSwLng) &
                              (Pokestop.latitude <= oNeLat) &
                              (Pokestop.longitude <= oNeLng)))
                     .dicts())
        elif lured:
            query = (query
                     .where(((Pokestop.last_updated >
                              datetime.datetime.utcfromtimestamp(timestamp / 1000))) &
                            ((Pokestop.latitude >= swLat) &
                             (Pokestop.longitude >= swLng) &
                             (Pokestop.latitude <= neLat) &
                             (Pokestop.longitude <= neLng)) &
                            (Pokestop.active_fort_modifier.is_null(False)))
                     .dicts())

        else:
            query = (query
                     .where((Pokestop.latitude >= swLat) &
                            (Pokestop.longitude >= swLng) &
                            (Pokestop.latitude <= neLat) &
                            (Pokestop.longitude <= neLng))
                     .dicts())

        # Performance:  disable the garbage collector prior to creating a
        # (potentially) large dict with append().
        gc.disable()

        now_date = datetime.datetime.utcnow()

        pokestops = {}
        pokestop_ids = []
        for p in query:
            if args.china:
                p['latitude'], p['longitude'] = \
                    transform_from_wgs_to_gcj(p['latitude'], p['longitude'])
            p['pokemon'] = []
            p['quest'] = {}
            if p['active_fort_modifier'] is None:
                p['active_fort_modifier'] = []
            else:
                p['active_fort_modifier'] = json.loads(p['active_fort_modifier'])
            pokestops[p['pokestop_id']] = p
            pokestop_ids.append(p['pokestop_id'])

        if len(pokestop_ids) > 0:
            pokemon = (PokestopMember
                       .select(
                           PokestopMember.encounter_id,
                           PokestopMember.pokestop_id,
                           PokestopMember.pokemon_id,
                           PokestopMember.disappear_time,
                           PokestopMember.gender,
                           PokestopMember.costume,
                           PokestopMember.form,
                           PokestopMember.weather_boosted_condition,
                           PokestopMember.last_modified,
                           PokestopMember.distance)
                       .where(PokestopMember.pokestop_id << pokestop_ids)
                       .where(PokestopMember.last_modified <= now_date)
                       .where(PokestopMember.disappear_time > now_date)
                       .distinct()
                       .dicts())

            for p in pokemon:
                p['pokemon_name'] = get_pokemon_name(p['pokemon_id'])
                pokestops[p['pokestop_id']]['pokemon'].append(p)

            details = (PokestopDetails
                       .select(
                           PokestopDetails.pokestop_id,
                           PokestopDetails.name,
                           PokestopDetails.url)
                       .where(PokestopDetails.pokestop_id << pokestop_ids)
                       .dicts())

            for d in details:
                pokestops[d['pokestop_id']]['name'] = d['name']
                pokestops[d['pokestop_id']]['url'] = d.get('url', '').replace('http://', 'https://')

            quests = (Quest
                      .select(
                          Quest.pokestop_id,
                          Quest.quest_type,
                          Quest.reward_type,
                          Quest.reward_item,
                          Quest.quest_json)
                      .where((Quest.pokestop_id << pokestop_ids) &
                             (Quest.expiration >= now_date))
                      .dicts())

            for q in quests:
                if q['quest_json'] is not None:
                    q['quest_json'] = json.loads(q['quest_json'])

                pokestops[q['pokestop_id']]['quest']['text'] = q['quest_type']
                pokestops[q['pokestop_id']]['quest']['type'] = q['reward_type']
                pokestops[q['pokestop_id']]['quest']['item'] = q['reward_item']
                pokestops[q['pokestop_id']]['quest']['icon'] = get_quest_icon(q['reward_type'], q['reward_item'])
                pokestops[q['pokestop_id']]['quest']['quest_json'] = q['quest_json']
                pokestops[q['pokestop_id']]['quest']['quest_text'] = get_quest_quest_text(q['quest_json'])
                pokestops[q['pokestop_id']]['quest']['reward_text'] = get_quest_reward_text(q['quest_json'])

        # Re-enable the GC.
        gc.enable()

        return pokestops

    @staticmethod
    def get_pokestop_details(id):
        try:
            details = (PokestopDetails
                       .select(
                           PokestopDetails.pokestop_id,
                           PokestopDetails.name,
                           PokestopDetails.description,
                           PokestopDetails.url)
                       .where(PokestopDetails.pokestop_id == id)
                       .dicts()
                       .get())
        except PokestopDetails.DoesNotExist:
            return None

        details['url'] = details.get('url', '').replace('http://', 'https://')

        return details

    @staticmethod
    def get_stop(id):
        try:
            result = (Pokestop
                      .select(Pokestop.pokestop_id,
                              PokestopDetails.name,
                              PokestopDetails.url,
                              PokestopDetails.description,
                              Pokestop.enabled,
                              Pokestop.latitude,
                              Pokestop.longitude,
                              Pokestop.last_modified,
                              Pokestop.lure_expiration,
                              Pokestop.active_fort_modifier,
                              Pokestop.active_pokemon_id,
                              Pokestop.active_pokemon_expiration,
                              Pokestop.last_updated)
                      .join(PokestopDetails, JOIN.LEFT_OUTER,
                            on=(Pokestop.pokestop_id == PokestopDetails.pokestop_id))
                      .where(Pokestop.pokestop_id == id)
                      .dicts()
                      .get())
        except Pokestop.DoesNotExist:
            return None

        if result['active_fort_modifier'] is None:
            result['active_fort_modifier'] = []
        else:
            result['active_fort_modifier'] = json.loads(result['active_fort_modifier'])

        result['url'] = result.get('url', '').replace('http://', 'https://')

        result['pokemon'] = []

        now_date = datetime.datetime.utcnow()

        pokemon = (PokestopMember
                   .select(
                       PokestopMember.encounter_id,
                       PokestopMember.pokestop_id,
                       PokestopMember.pokemon_id,
                       PokestopMember.disappear_time,
                       PokestopMember.gender,
                       PokestopMember.costume,
                       PokestopMember.form,
                       PokestopMember.weather_boosted_condition,
                       PokestopMember.last_modified,
                       PokestopMember.distance)
                   .where(PokestopMember.pokestop_id == id)
                   .where(PokestopMember.last_modified <= now_date)
                   .where(PokestopMember.disappear_time > now_date)
                   .distinct()
                   .dicts())

        for p in pokemon:
            p['pokemon_name'] = get_pokemon_name(p['pokemon_id'])

            result['pokemon'].append(p)

        result['quest'] = {}

        quest = (Quest
                 .select(
                     Quest.quest_type,
                     Quest.reward_type,
                     Quest.reward_item,
                     Quest.quest_json)
                 .where((Quest.pokestop_id == id) & (Quest.expiration >= now_date))
                 .dicts())

        for q in quest:
            if q['quest_json'] is not None:
                q['quest_json'] = json.loads(q['quest_json'])

            result['quest']['text'] = q['quest_type']
            result['quest']['type'] = q['reward_type']
            result['quest']['item'] = q['reward_item']
            result['quest']['icon'] = get_quest_icon(q['reward_type'], q['reward_item'])
            result['quest']['quest_json'] = q['quest_json']
            result['quest']['quest_text'] = get_quest_quest_text(q['quest_json'])
            result['quest']['reward_text'] = get_quest_reward_text(q['quest_json'])

        return result

    @staticmethod
    def get_nearby_pokestops(lat, lng, dist, questless, maxpoints, geofence_name, scheduled_points, geofences):
        pokestops = {}
        with Pokestop.database().execution_context():
            query = (Pokestop.select(
                Pokestop.latitude, Pokestop.longitude, Pokestop.pokestop_id).dicts())

            lat1 = lat - 0.1
            lat2 = lat + 0.1
            lng1 = lng - 0.1
            lng2 = lng + 0.1

            if geofence_name != "":
                lat1, lng1, lat2, lng2 = geofences.get_boundary_coords(geofence_name)

            minlat = min(lat1, lat2)
            maxlat = max(lat1, lat2)
            minlng = min(lng1, lng2)
            maxlng = max(lng1, lng2)

            if dist > 0:
                query = (query
                         .where((((Pokestop.latitude >= minlat) &
                                  (Pokestop.longitude >= minlng) &
                                  (Pokestop.latitude <= maxlat) &
                                  (Pokestop.longitude <= maxlng))))
                         .dicts())

            if len(scheduled_points) > 0:
                query = (query
                         .where(Pokestop.pokestop_id.not_in(scheduled_points))
                         .dicts())

            queryDict = query.dicts()

            pokestop_quest_ids = []

            if questless:
                pokestop_ids = []
                for p in queryDict:
                    pokestop_ids.append(p['pokestop_id'])

                now_date = datetime.datetime.utcnow()

                if len(pokestop_ids) > 0:
                    quests = (Quest
                              .select(
                                  Quest.pokestop_id,
                                  Quest.quest_type,
                                  Quest.reward_type,
                                  Quest.reward_item,
                                  Quest.quest_json)
                              .where((Quest.pokestop_id << pokestop_ids) & (Quest.expiration >= now_date))
                              .dicts())

                    for q in quests:
                        pokestop_quest_ids.append(q['pokestop_id'])

            if len(queryDict) > 0 and geofences.is_enabled():
                queryDict = geofences.get_geofenced_results(queryDict, geofence_name)

            for p in queryDict:
                key = p['pokestop_id']
                latitude = round(p['latitude'], 5)
                longitude = round(p['longitude'], 5)
                distance = geopy.distance.vincenty((lat, lng), (latitude, longitude)).km
                if (not questless or p['pokestop_id'] not in pokestop_quest_ids) and (dist == 0 or distance <= dist):
                    pokestops[key] = {
                        'latitude': latitude,
                        'longitude': longitude,
                        'distance': distance,
                        'key': key
                    }
            orderedpokestops = OrderedDict(sorted(pokestops.items(), key=lambda x: x[1]['distance']))

            maxlength = len(orderedpokestops)
            if (not isinstance(questless, (bool)) and maxlength > questless):
                maxlength = questless

            if (not isinstance(maxpoints, (bool)) and maxlength > maxpoints):
                maxlength = maxpoints

            while len(orderedpokestops) > maxlength:
                orderedpokestops.popitem()

            result = []
            while len(orderedpokestops) > 0:
                value = list(orderedpokestops.items())[0][1]
                result.append((value['latitude'], value['longitude'], value['key']))
                newlat = value['latitude']
                newlong = value['longitude']
                orderedpokestops.popitem(last=False)
                orderedpokestops = OrderedDict(sorted(orderedpokestops.items(), key=lambda x: geopy.distance.vincenty((newlat, newlong), (x[1]['latitude'], x[1]['longitude'])).km))

        return result


class Gym(LatLongModel):
    gym_id = Utf8mb4CharField(primary_key=True, max_length=50)
    team_id = SmallIntegerField()
    guard_pokemon_id = SmallIntegerField()
    slots_available = SmallIntegerField()
    enabled = BooleanField()
    park = BooleanField(default=False)
    latitude = DoubleField()
    longitude = DoubleField()
    total_cp = SmallIntegerField()
    last_modified = DateTimeField(index=True)
    last_scanned = DateTimeField(default=datetime.datetime.utcnow, index=True)
    is_in_battle = BooleanField(default=False)
    is_ex_raid_eligible = BooleanField(default=False)

    class Meta:
        indexes = ((('latitude', 'longitude'), False),)

    @staticmethod
    def get_gyms(swLat, swLng, neLat, neLng, timestamp=0, oSwLat=None,
                 oSwLng=None, oNeLat=None, oNeLng=None):
        if not (swLat and swLng and neLat and neLng):
            results = (Gym
                       .select()
                       .dicts())
        elif timestamp > 0:
            # If timestamp is known only send last scanned Gyms.
            results = (Gym
                       .select()
                       .where(((Gym.last_scanned >
                                datetime.datetime.utcfromtimestamp(timestamp / 1000)) &
                               (Gym.latitude >= swLat) &
                               (Gym.longitude >= swLng) &
                               (Gym.latitude <= neLat) &
                               (Gym.longitude <= neLng)))
                       .dicts())
        elif oSwLat and oSwLng and oNeLat and oNeLng:
            # Send gyms in view but exclude those within old boundaries. Only
            # send newly uncovered gyms.
            results = (Gym
                       .select()
                       .where(((Gym.latitude >= swLat) &
                               (Gym.longitude >= swLng) &
                               (Gym.latitude <= neLat) &
                               (Gym.longitude <= neLng)) &
                              ~((Gym.latitude >= oSwLat) &
                                (Gym.longitude >= oSwLng) &
                                (Gym.latitude <= oNeLat) &
                                (Gym.longitude <= oNeLng)))
                       .dicts())

        else:
            results = (Gym
                       .select()
                       .where((Gym.latitude >= swLat) &
                              (Gym.longitude >= swLng) &
                              (Gym.latitude <= neLat) &
                              (Gym.longitude <= neLng))
                       .dicts())

        # Performance:  disable the garbage collector prior to creating a
        # (potentially) large dict with append().
        gc.disable()

        gyms = {}
        gym_ids = []
        for g in results:
            if args.china:
                g['latitude'], g['longitude'] = \
                transform_from_wgs_to_gcj(g['latitude'], g['longitude'])
            g['name'] = None
            g['pokemon'] = []
            g['raid'] = None
            gyms[g['gym_id']] = g
            gym_ids.append(g['gym_id'])

        if len(gym_ids) > 0:
            pokemon = (GymMember
                       .select(
                           GymMember.gym_id,
                           GymPokemon.cp.alias('pokemon_cp'),
                           GymMember.cp_decayed,
                           GymMember.deployment_time,
                           GymMember.last_scanned,
                           GymPokemon.pokemon_id,
                           GymPokemon.costume,
                           GymPokemon.form,
                           GymPokemon.shiny)
                       .join(Gym, on=(GymMember.gym_id == Gym.gym_id))
                       .join(GymPokemon, on=(GymMember.pokemon_uid ==
                                             GymPokemon.pokemon_uid))
                       .where(GymMember.gym_id << gym_ids)
                       .where(GymMember.last_scanned > Gym.last_modified)
                       .distinct()
                       .dicts())

            for p in pokemon:
                p['pokemon_name'] = get_pokemon_name(p['pokemon_id'])
                gyms[p['gym_id']]['pokemon'].append(p)

            details = (GymDetails
                       .select(
                           GymDetails.gym_id,
                           GymDetails.name,
                           GymDetails.url)
                       .where(GymDetails.gym_id << gym_ids)
                       .dicts())

            for d in details:
                gyms[d['gym_id']]['name'] = d['name']
                gyms[d['gym_id']]['url'] = d['url'].replace('http://', 'https://')

            raids = (Raid
                     .select()
                     .where(Raid.gym_id << gym_ids)
                     .dicts())

            for r in raids:
                if r['pokemon_id']:
                    r['pokemon_name'] = get_pokemon_name(r['pokemon_id'])
                    r['pokemon_types'] = get_pokemon_types(r['pokemon_id'])
                gyms[r['gym_id']]['raid'] = r

        # Re-enable the GC.
        gc.enable()

        return gyms

    @staticmethod
    def get_raids():
        raids = (Raid
                 .select(Gym.latitude,
                         Gym.longitude,
                         Gym.is_ex_raid_eligible,
                         GymDetails.name,
                         GymDetails.url,
                         Raid.level,
                         Raid.pokemon_id,
                         Raid.start,
                         Raid.end,
                         Raid.last_scanned)
                 .join(Gym, on=(Raid.gym_id == Gym.gym_id))
                 .join(GymDetails, on=(GymDetails.gym_id == Gym.gym_id))
                 .where(Raid.end > datetime.datetime.utcnow())
                 .order_by(Raid.end.asc())
                 .dicts())

        for r in raids:
            if r['pokemon_id']:
                r['pokemon_name'] = get_pokemon_name(r['pokemon_id'])
            r['url'] = r.get('url', '').replace('http://', 'https://')

        return raids

    @staticmethod
    def get_gym_details(id):
        try:
            details = (GymDetails
                       .select(
                           GymDetails.gym_id,
                           GymDetails.name,
                           GymDetails.description,
                           GymDetails.url)
                       .where(GymDetails.gym_id == id)
                       .dicts()
                       .get())
        except GymDetails.DoesNotExist:
            return None

        details['url'] = details.get('url', '').replace('http://', 'https://')

        return details

    @staticmethod
    def get_gym(id):

        try:
            result = (Gym
                      .select(Gym.gym_id,
                              Gym.team_id,
                              GymDetails.name,
                              GymDetails.description,
                              GymDetails.url,
                              Gym.guard_pokemon_id,
                              Gym.slots_available,
                              Gym.latitude,
                              Gym.longitude,
                              Gym.last_modified,
                              Gym.last_scanned,
                              Gym.total_cp,
                              Gym.is_in_battle,
                              Gym.is_ex_raid_eligible)
                      .join(GymDetails, JOIN.LEFT_OUTER,
                            on=(Gym.gym_id == GymDetails.gym_id))
                      .where(Gym.gym_id == id)
                      .dicts()
                      .get())
        except Gym.DoesNotExist:
            return None

        result['guard_pokemon_name'] = get_pokemon_name(
            result['guard_pokemon_id']) if result['guard_pokemon_id'] else ''

        result['url'] = result.get('url', '').replace('http://', 'https://')

        result['pokemon'] = []

        pokemon = (GymMember
                   .select(GymPokemon.cp.alias('pokemon_cp'),
                           GymMember.cp_decayed,
                           GymMember.deployment_time,
                           GymMember.last_scanned,
                           GymPokemon.pokemon_id,
                           GymPokemon.pokemon_uid,
                           GymPokemon.move_1,
                           GymPokemon.move_2,
                           GymPokemon.iv_attack,
                           GymPokemon.iv_defense,
                           GymPokemon.iv_stamina,
                           GymPokemon.costume,
                           GymPokemon.form,
                           GymPokemon.shiny)
                   .join(Gym, on=(GymMember.gym_id == Gym.gym_id))
                   .join(GymPokemon,
                         on=(GymMember.pokemon_uid == GymPokemon.pokemon_uid))
                   .where(GymMember.gym_id == id)
                   .where(GymMember.last_scanned > Gym.last_modified)
                   .order_by(GymMember.deployment_time.desc())
                   .distinct()
                   .dicts())

        for p in pokemon:
            p['pokemon_name'] = get_pokemon_name(p['pokemon_id'])

            p['move_1_name'] = get_move_name(p['move_1'])
            p['move_1_damage'] = get_move_damage(p['move_1'])
            p['move_1_energy'] = get_move_energy(p['move_1'])
            p['move_1_type'] = get_move_type(p['move_1'])

            p['move_2_name'] = get_move_name(p['move_2'])
            p['move_2_damage'] = get_move_damage(p['move_2'])
            p['move_2_energy'] = get_move_energy(p['move_2'])
            p['move_2_type'] = get_move_type(p['move_2'])

            result['pokemon'].append(p)

        try:
            raid = Raid.select(Raid).where(Raid.gym_id == id).dicts().get()
            if raid['pokemon_id']:
                raid['pokemon_name'] = get_pokemon_name(raid['pokemon_id'])
                raid['pokemon_types'] = get_pokemon_types(raid['pokemon_id'])
            result['raid'] = raid
        except Raid.DoesNotExist:
            pass

        return result

    @staticmethod
    def set_gyms_in_park(gyms, park):
        gym_ids = [gym['gym_id'] for gym in gyms]
        Gym.update(park=park).where(Gym.gym_id << gym_ids).execute()

    @staticmethod
    def get_gyms_park(id):
        with Gym.database().execution_context():
            gym_by_id = Gym.select(Gym.park).where(
                Gym.gym_id == id).dicts()
            if gym_by_id:
                return gym_by_id[0]['park']
        return False

    @staticmethod
    def get_nearby_gyms(lat, lng, dist, teleport_ignore, raidless, maxpoints, geofence_name, scheduled_points, geofences, exraidonly, oldest_first):
        gyms = {}
        with Gym.database().execution_context():
            query = (Gym.select(
                Gym.latitude, Gym.longitude, Gym.gym_id, Gym.last_scanned).dicts())

            if geofence_name != "":
                lat1, lng1, lat2, lng2 = geofences.get_boundary_coords(geofence_name)

                minlat = min(lat1, lat2)
                maxlat = max(lat1, lat2)
                minlng = min(lng1, lng2)
                maxlng = max(lng1, lng2)

                query = (query
                         .where((Gym.latitude >= minlat) &
                                (Gym.longitude >= minlng) &
                                (Gym.latitude <= maxlat) &
                                (Gym.longitude <= maxlng) &
                                (Gym.last_scanned < datetime.datetime.utcnow() - datetime.timedelta(seconds=60)))
                         .dicts())
            else:
                query = (query.where(Gym.last_scanned < datetime.datetime.utcnow() - datetime.timedelta(seconds=60)).dicts())

            if len(scheduled_points) > 0:
                query = (query
                         .where(Gym.gym_id.not_in(scheduled_points))
                         .dicts())

            if exraidonly:
                query = (query
                         .where(Gym.is_ex_raid_eligible)
                         .dicts())

            queryDict = query.order_by(Gym.last_scanned.asc()).dicts()

            gym_ids = []
            egg_todo = []
            for g in queryDict:
                gym_ids.append(g['gym_id'])

            if raidless and len(gym_ids) > 0:
                raids = (Raid
                         .select()
                         .where(Raid.gym_id << gym_ids)
                         .dicts())

                for r in raids:
                    if not isinstance(raidless, (bool)):
                        if (r['pokemon_id'] is None and r['end'] > datetime.datetime.utcnow()) and r['start'] < datetime.datetime.utcnow():
                            egg_todo.append(r['gym_id'])
                    if (r['pokemon_id'] and r['end'] > datetime.datetime.utcnow()) or r['start'] > datetime.datetime.utcnow():
                        gym_ids.remove(r['gym_id'])
                    elif r['pokemon_id'] is None and r['end'] > datetime.datetime.utcnow() and r['start'] < datetime.datetime.utcnow():
                        egg_todo.append(r['gym_id'])

            if len(egg_todo) > 0:
                gym_ids = egg_todo[:]
            elif (not isinstance(raidless, (bool)) and len(gym_ids) > raidless):
                gym_ids = gym_ids[:raidless]

            if len(queryDict) > 0 and geofences.is_enabled():
                queryDict = geofences.get_geofenced_results(queryDict, geofence_name)

            for g in queryDict:
                key = g['gym_id']
                latitude = round(g['latitude'], 5)
                longitude = round(g['longitude'], 5)
                distance = geopy.distance.vincenty((lat, lng), (latitude, longitude)).km
                if g['gym_id'] in gym_ids and (dist == 0 or distance <= dist):
                    gyms[key] = {
                        'latitude': latitude,
                        'longitude': longitude,
                        'distance': distance,
                        'key': key,
                        'last_scanned': g['last_scanned'],
                    }
            if oldest_first:
                orderedgyms = OrderedDict(sorted(gyms.items(), key=lambda x: x[1]['last_scanned']))
            else:
                orderedgyms = OrderedDict(sorted(gyms.items(), key=lambda x: x[1]['distance']))

            newlat = 0
            newlong = 0

            maxlength = len(orderedgyms)
            if (not isinstance(raidless, (bool)) and maxlength > raidless):
                maxlength = raidless

            if (not isinstance(maxpoints, (bool)) and maxlength > maxpoints):
                maxlength = maxpoints

            while len(orderedgyms) > maxlength:
                orderedgyms.popitem()

            result = []
            while len(orderedgyms) > 0:
                value = list(orderedgyms.items())[0][1]
                orderedgyms.popitem(last=False)
                if len(result) == 0 or geopy.distance.vincenty((newlat, newlong), (value['latitude'], value['longitude'])).km * 1000 > teleport_ignore:
                    result.append((value['latitude'], value['longitude'], value['key']))
                    newlat = value['latitude']
                    newlong = value['longitude']
                if not oldest_first:
                    orderedgyms = OrderedDict(sorted(orderedgyms.items(), key=lambda x: geopy.distance.vincenty((newlat, newlong), (x[1]['latitude'], x[1]['longitude'])).km))

        return result


class Raid(BaseModel):
    gym_id = Utf8mb4CharField(primary_key=True, max_length=50)
    level = IntegerField(index=True)
    spawn = DateTimeField(index=True)
    start = DateTimeField(index=True)
    end = DateTimeField(index=True)
    pokemon_id = SmallIntegerField(null=True)
    cp = IntegerField(null=True)
    move_1 = SmallIntegerField(null=True)
    move_2 = SmallIntegerField(null=True)
    form = SmallIntegerField(null=True)
    last_scanned = DateTimeField(default=datetime.datetime.utcnow, index=True)


class LocationAltitude(LatLongModel):
    cellid = UBigIntegerField(primary_key=True)
    latitude = DoubleField()
    longitude = DoubleField()
    last_modified = DateTimeField(index=True, default=datetime.datetime.utcnow,
                                  null=True)
    altitude = DoubleField()

    class Meta:
        indexes = ((('latitude', 'longitude'), False),)

    # DB format of a new location altitude
    @staticmethod
    def new_loc(loc, altitude):
        return {'cellid': cellid(loc),
                'latitude': loc[0],
                'longitude': loc[1],
                'altitude': altitude}

    # find a nearby altitude from the db
    # looking for one within 140m
    @staticmethod
    def get_nearby_altitude(loc):
        n, e, s, w = hex_bounds(loc, radius=0.14)  # 140m

        # Get all location altitudes in that box.
        query = (LocationAltitude
                 .select()
                 .where((LocationAltitude.latitude <= n) &
                        (LocationAltitude.latitude >= s) &
                        (LocationAltitude.longitude >= w) &
                        (LocationAltitude.longitude <= e))
                 .dicts())

        altitude = None
        if len(list(query)):
            altitude = query[0]['altitude']

        return altitude

    @staticmethod
    def save_altitude(loc, altitude):
        InsertQuery(
            LocationAltitude,
            rows=[LocationAltitude.new_loc(loc, altitude)]).upsert().execute()


class PlayerLocale(BaseModel):
    location = Utf8mb4CharField(primary_key=True, max_length=50, index=True)
    country = Utf8mb4CharField(max_length=2)
    language = Utf8mb4CharField(max_length=2)
    timezone = Utf8mb4CharField(max_length=50)

    @staticmethod
    def get_locale(location):
        locale = None
        with PlayerLocale.database().execution_context():
            try:
                query = PlayerLocale.get(PlayerLocale.location == location)
                locale = {
                    'country': query.country,
                    'language': query.language,
                    'timezone': query.timezone
                }
            except PlayerLocale.DoesNotExist:
                log.debug('This location is not yet in PlayerLocale DB table.')
        return locale


class DeviceWorker(LatLongModel):
    deviceid = Utf8mb4CharField(primary_key=True, max_length=100, index=True)
    name = Utf8mb4CharField(max_length=100, default="")
    username = Utf8mb4CharField(max_length=100, default="")
    latitude = DoubleField()
    longitude = DoubleField()
    centerlatitude = DoubleField()
    centerlongitude = DoubleField()
    radius = SmallIntegerField(default=0)
    step = SmallIntegerField(default=0)
    last_scanned = DateTimeField(index=True)
    last_updated = DateTimeField(index=True, default=datetime.datetime.utcnow)
    scans = UBigIntegerField(default=0)
    direction = Utf8mb4CharField(max_length=1, default="U")
    fetching = Utf8mb4CharField(max_length=50, default='IDLE')
    scanning = SmallIntegerField(default=0)
    endpoint = Utf8mb4CharField(max_length=2000, default="")
    requestedEndpoint = Utf8mb4CharField(max_length=2000, default="")
    pogoversion = Utf8mb4CharField(max_length=10, default="")

    @staticmethod
    def get_by_id(id, latitude=0, longitude=0):
        with DeviceWorker.database().execution_context():
            query = (DeviceWorker
                     .select()
                     .where(DeviceWorker.deviceid == id)
                     .dicts())

            result = query[0] if query else {
                'deviceid': id,
                'name': '',
                'username': '',
                'latitude': latitude,
                'longitude': longitude,
                'centerlatitude': latitude,
                'centerlongitude': longitude,
                'last_scanned': None,  # Null value used as new flag.
                'last_updated': datetime.datetime.utcnow(),  # Null value used as new flag.
                'radius': 0,
                'step': 0,
                'scans': 0,
                'direction': 'U',
                'fetching': 'IDLE',
                'scanning': 0,
                'endpoint': '',
                'pogoversion': ''
            }
        return result

    @staticmethod
    def get_existing_by_id(id):
        try:
            with DeviceWorker.database().execution_context():
                query = (DeviceWorker
                         .select()
                         .where(DeviceWorker.deviceid == id)
                         .dicts())

                result = query[0]
        except DeviceWorker.DoesNotExist:
            result = None
        return result

    @staticmethod
    def get_all():
        with DeviceWorker.database().execution_context():
            query = (DeviceWorker
                     .select()
                     .dicts())

        return list(query)

    @staticmethod
    def get_active():
        with DeviceWorker.database().execution_context():
            query = (DeviceWorker
                     .select(DeviceWorker.deviceid,
                             DeviceWorker.name,
                             DeviceWorker.username,
                             DeviceWorker.latitude,
                             DeviceWorker.longitude,
                             DeviceWorker.last_scanned,
                             DeviceWorker.last_updated,
                             DeviceWorker.scans,
                             DeviceWorker.fetching,
                             DeviceWorker.scanning,
                             DeviceWorker.endpoint,
                             DeviceWorker.requestedEndpoint,
                             DeviceWorker.pogoversion)
                     .where((DeviceWorker.scanning == 1) |
                            (DeviceWorker.fetching != 'IDLE'))
                     .dicts())
        if args.china:
            for result in query:
                result['latitude'], result['longitude'] = \
                    transform_from_wgs_to_gcj(
                        result['latitude'], result['longitude'])
        return list(query)

    @staticmethod
    def get_active_by_id(id):
        with DeviceWorker.database().execution_context():
            query = (DeviceWorker
                     .select(DeviceWorker.deviceid,
                             DeviceWorker.name,
                             DeviceWorker.username,
                             DeviceWorker.latitude,
                             DeviceWorker.longitude,
                             DeviceWorker.last_scanned,
                             DeviceWorker.last_updated,
                             DeviceWorker.scans,
                             DeviceWorker.fetching,
                             DeviceWorker.scanning,
                             DeviceWorker.endpoint,
                             DeviceWorker.requestedEndpoint,
                             DeviceWorker.pogoversion)
                     .where(DeviceWorker.id == id)
                     .dicts())

        return list(query)


class ScannedLocation(LatLongModel):
    cellid = UBigIntegerField(primary_key=True)
    latitude = DoubleField()
    longitude = DoubleField()
    last_modified = DateTimeField(
        index=True, default=datetime.datetime.utcnow, null=True)
    # Marked true when all five bands have been completed.
    done = BooleanField(default=False)

    # Five scans/hour is required to catch all spawns.
    # Each scan must be at least 12 minutes from the previous check,
    # with a 2 minute window during which the scan can be done.

    # Default of -1 is for bands not yet scanned.
    band1 = SmallIntegerField(default=-1)
    band2 = SmallIntegerField(default=-1)
    band3 = SmallIntegerField(default=-1)
    band4 = SmallIntegerField(default=-1)
    band5 = SmallIntegerField(default=-1)

    # midpoint is the center of the bands relative to band 1.
    # If band 1 is 10.4 minutes, and band 4 is 34.0 minutes, midpoint
    # is -0.2 minutes in minsec.  Extra 10 seconds in case of delay in
    # recording now time.
    midpoint = SmallIntegerField(default=0)

    # width is how wide the valid window is. Default is 0, max is 2 minutes.
    # If band 1 is 10.4 minutes, and band 4 is 34.0 minutes, midpoint
    # is 0.4 minutes in minsec.
    width = SmallIntegerField(default=0)

    fortradius = UBigIntegerField(default=450)
    monradius = UBigIntegerField(default=70)
    scanningforts = SmallIntegerField(default=0)

    class Meta:
        indexes = ((('latitude', 'longitude'), False),)
        constraints = [Check('band1 >= -1'), Check('band1 < 3600'),
                       Check('band2 >= -1'), Check('band2 < 3600'),
                       Check('band3 >= -1'), Check('band3 < 3600'),
                       Check('band4 >= -1'), Check('band4 < 3600'),
                       Check('band5 >= -1'), Check('band5 < 3600'),
                       Check('midpoint >= -130'), Check('midpoint <= 130'),
                       Check('width >= 0'), Check('width <= 130')]

    @staticmethod
    def get_recent(swLat, swLng, neLat, neLng, timestamp=0, oSwLat=None,
                   oSwLng=None, oNeLat=None, oNeLng=None):
        activeTime = (datetime.datetime.utcnow() - datetime.timedelta(minutes=15))
        if timestamp > 0:
            query = (ScannedLocation
                     .select()
                     .where(((ScannedLocation.last_modified >=
                              datetime.datetime.utcfromtimestamp(timestamp / 1000))) &
                            (ScannedLocation.latitude >= swLat) &
                            (ScannedLocation.longitude >= swLng) &
                            (ScannedLocation.latitude <= neLat) &
                            (ScannedLocation.longitude <= neLng))
                     .dicts())
        elif oSwLat and oSwLng and oNeLat and oNeLng:
            # Send scannedlocations in view but exclude those within old
            # boundaries. Only send newly uncovered scannedlocations.
            query = (ScannedLocation
                     .select()
                     .where((((ScannedLocation.last_modified >= activeTime)) &
                             (ScannedLocation.latitude >= swLat) &
                             (ScannedLocation.longitude >= swLng) &
                             (ScannedLocation.latitude <= neLat) &
                             (ScannedLocation.longitude <= neLng)) &
                            ~(((ScannedLocation.last_modified >= activeTime)) &
                              (ScannedLocation.latitude >= oSwLat) &
                              (ScannedLocation.longitude >= oSwLng) &
                              (ScannedLocation.latitude <= oNeLat) &
                              (ScannedLocation.longitude <= oNeLng)))
                     .dicts())
        else:
            query = (ScannedLocation
                     .select()
                     .where((ScannedLocation.last_modified >= activeTime) &
                            (ScannedLocation.latitude >= swLat) &
                            (ScannedLocation.longitude >= swLng) &
                            (ScannedLocation.latitude <= neLat) &
                            (ScannedLocation.longitude <= neLng))
                     .order_by(ScannedLocation.last_modified.asc())
                     .dicts())

        if args.china:
            for result in query:
                result['latitude'], result['longitude'] = \
                    transform_from_wgs_to_gcj(
                        result['latitude'], result['longitude'])
        return list(query)

    # DB format of a new location.
    @staticmethod
    def new_loc(loc):
        return {'cellid': cellid(loc),
                'latitude': loc[0],
                'longitude': loc[1],
                'done': False,
                'band1': -1,
                'band2': -1,
                'band3': -1,
                'band4': -1,
                'band5': -1,
                'width': 0,
                'midpoint': 0,
                'fortradius': 450,
                'monradius': 70,
                'scanningforts': 0,
                'last_modified': None}

    # Used to update bands.
    @staticmethod
    def db_format(scan, band, nowms):
        scan.update({'band' + str(band): nowms})
        scan['done'] = reduce(lambda x, y: x and (
            scan['band' + str(y)] > -1), range(1, 6), True)
        return scan

    # Shorthand helper for DB dict.
    @staticmethod
    def _q_init(scan, start, end, kind, sp_id=None):
        return {'loc': scan['loc'], 'kind': kind, 'start': start, 'end': end,
                'step': scan['step'], 'sp': sp_id}

    @staticmethod
    def get_by_cellids(cellids):
        d = {}
        with ScannedLocation.database().execution_context():
            query = (ScannedLocation
                     .select()
                     .where(ScannedLocation.cellid << cellids)
                     .dicts())

            for sl in list(query):
                key = "{}".format(sl['cellid'])
                d[key] = sl
        return d

    @staticmethod
    def find_in_locs(loc, locs):
        key = "{}".format(cellid(loc))
        return locs[key] if key in locs else ScannedLocation.new_loc(loc)

    # Return value of a particular scan from loc, or default dict if not found.
    @staticmethod
    def get_by_loc(loc):
        with ScannedLocation.database().execution_context():
            query = (ScannedLocation
                     .select()
                     .where(ScannedLocation.cellid == cellid(loc))
                     .dicts())
            result = query[0] if len(
                list(query)) else ScannedLocation.new_loc(loc)
        return result

    # Check if spawnpoints in a list are in any of the existing
    # spannedlocation records.  Otherwise, search through the spawnpoint list
    # and update scan_spawn_point dict for DB bulk upserting.
    @staticmethod
    def link_spawn_points(scans, initial, spawn_points, distance):
        index = 0
        scan_spawn_point = {}
        for cell, scan in scans.iteritems():
            # Difference in degrees at the equator for 70m is actually 0.00063
            # degrees and gets smaller the further north or south you go
            deg_at_lat = 0.0007 / math.cos(math.radians(scan['loc'][0]))
            for sp in spawn_points:
                if (abs(sp['latitude'] - scan['loc'][0]) > 0.0008 or
                        abs(sp['longitude'] - scan['loc'][1]) > deg_at_lat):
                    continue
                if in_radius((sp['latitude'], sp['longitude']),
                             scan['loc'], distance * 1000):
                    scan_spawn_point[index] = {
                        'spawnpoint': sp['id'],
                        'scannedlocation': cell}
                    index += 1
        return scan_spawn_point

    # Return list of dicts for upcoming valid band times.
    @staticmethod
    def linked_spawn_points(cell):

        # Unable to use a normal join, since MySQL produces foreignkey
        # constraint errors when trying to upsert fields that are foreignkeys
        # on another table
        with SpawnPoint.database().execution_context():
            query = (SpawnPoint
                     .select()
                     .join(ScanSpawnPoint)
                     .join(ScannedLocation)
                     .where(ScannedLocation.cellid == cell).dicts())
            result = list(query)
        return result

    # Return list of dicts for upcoming valid band times.
    @staticmethod
    def get_cell_to_linked_spawn_points(cellids, location_change_date):
        # Get all spawnpoints from the hive's cells
        sp_from_cells = (ScanSpawnPoint
                         .select(ScanSpawnPoint.spawnpoint)
                         .where(ScanSpawnPoint.scannedlocation << cellids)
                         .alias('spcells'))
        # A new SL (new ones are created when the location changes) or
        # it can be a cell from another active hive
        one_sp_scan = (
            ScanSpawnPoint.select(
                ScanSpawnPoint.spawnpoint,
                fn.MAX(ScanSpawnPoint.scannedlocation).alias('cellid'))
            .join(
                sp_from_cells,
                on=sp_from_cells.c.spawnpoint_id == ScanSpawnPoint.spawnpoint)
            .join(
                ScannedLocation,
                on=(ScannedLocation.cellid == ScanSpawnPoint.scannedlocation))
            .where(((ScannedLocation.last_modified >= (location_change_date)) &
                    (ScannedLocation.last_modified >
                     (datetime.datetime.utcnow() - datetime.timedelta(minutes=60)))) | (
                         ScannedLocation.cellid << cellids))
            .group_by(ScanSpawnPoint.spawnpoint).alias('maxscan'))
        # As scan locations overlap,spawnpoints can belong to up to 3 locations
        # This sub-query effectively assigns each SP to exactly one location.
        ret = {}
        with SpawnPoint.database().execution_context():
            query = (SpawnPoint
                     .select(SpawnPoint, one_sp_scan.c.cellid)
                     .join(one_sp_scan, on=(SpawnPoint.id ==
                                            one_sp_scan.c.spawnpoint_id))
                     .where(one_sp_scan.c.cellid << cellids)
                     .dicts())
            spawns = list(query)
            for item in spawns:
                if item['cellid'] not in ret:
                    ret[item['cellid']] = []
                ret[item['cellid']].append(item)

        return ret

    # Return list of dicts for upcoming valid band times.
    @staticmethod
    def get_times(scan, now_date, scanned_locations):
        s = ScannedLocation.find_in_locs(scan['loc'], scanned_locations)
        if s['done']:
            return []

        max = 3600 * 2 + 250  # Greater than maximum possible value.
        min = {'end': max}

        nowms = date_secs(now_date)
        if s['band1'] == -1:
            return [ScannedLocation._q_init(scan, nowms, nowms + 3599, 'band')]

        # Find next window.
        basems = s['band1']
        for i in range(2, 6):
            ms = s['band' + str(i)]

            # Skip bands already done.
            if ms > -1:
                continue

            radius = 120 - s['width'] / 2
            end = (basems + s['midpoint'] + radius + (i - 1) * 720 - 10) % 3600
            end = end if end >= nowms else end + 3600

            if end < min['end']:
                min = ScannedLocation._q_init(scan, end - radius * 2 + 10, end,
                                              'band')

        return [min] if min['end'] < max else []

    # Checks if now falls within an unfilled band for a scanned location.
    # Returns the updated scan location dict.
    @staticmethod
    def update_band(scan, now_date):

        scan['last_modified'] = now_date

        if scan['done']:
            return scan

        now_secs = date_secs(now_date)
        if scan['band1'] == -1:
            return ScannedLocation.db_format(scan, 1, now_secs)

        # Calculate if number falls in band with remaining points.
        basems = scan['band1']
        delta = (now_secs - basems - scan['midpoint']) % 3600
        band = int(round(delta / 12 / 60.0) % 5) + 1

        # Check if that band is already filled.
        if scan['band' + str(band)] > -1:
            return scan

        # Check if this result falls within the band's 2 minute window.
        offset = (delta + 1080) % 720 - 360
        if abs(offset) > 120 - scan['width'] / 2:
            return scan

        # Find band midpoint/width.
        scan = ScannedLocation.db_format(scan, band, now_secs)
        bts = [scan['band' + str(i)] for i in range(1, 6)]
        bts = filter(lambda ms: ms > -1, bts)
        bts_delta = list(map(lambda ms: (ms - basems) % 3600, bts))
        bts_offsets = list(map(lambda ms: (ms + 1080) % 720 - 360, bts_delta))
        min_scan = min(bts_offsets)
        max_scan = max(bts_offsets)
        scan['width'] = max_scan - min_scan
        scan['midpoint'] = (max_scan + min_scan) / 2

        return scan

    @staticmethod
    def get_bands_filled_by_cellids(cellids):
        with SpawnPoint.database().execution_context():
            result = int(
                ScannedLocation.select(
                    fn.SUM(
                        case(ScannedLocation.band1, ((-1, 0),), 1) +
                        case(ScannedLocation.band2, ((-1, 0),), 1) + case(
                            ScannedLocation.band3, ((-1, 0),), 1) + case(
                                ScannedLocation.band4, ((-1, 0),), 1) + case(
                                    ScannedLocation.band5, ((-1, 0),), 1))
                    .alias('band_count'))
                .where(ScannedLocation.cellid << cellids).scalar() or 0)
        return result

    @staticmethod
    def reset_bands(scan_loc):
        scan_loc['done'] = False
        scan_loc['last_modified'] = datetime.datetime.utcnow()
        for i in range(1, 6):
            scan_loc['band' + str(i)] = -1

    @staticmethod
    def select_in_hex(locs):
        # There should be a way to delegate this to SpawnPoint.select_in_hex,
        # but w/e.
        cells = []
        for i, e in enumerate(locs):
            cells.append(cellid(e[1]))

        in_hex = []
        # Get all spawns for the locations.
        with SpawnPoint.database().execution_context():
            sp = list(ScannedLocation
                      .select()
                      .where(ScannedLocation.cellid << cells)
                      .dicts())

            # For each spawn work out if it is in the hex
            # (clipping the diagonals).
            for spawn in sp:
                in_hex.append(spawn)

        return in_hex


class MainWorker(BaseModel):
    worker_name = Utf8mb4CharField(primary_key=True, max_length=50)
    message = TextField(null=True, default="")
    method = Utf8mb4CharField(max_length=50)
    last_modified = DateTimeField(index=True)
    accounts_working = IntegerField()
    accounts_captcha = IntegerField()
    accounts_failed = IntegerField()
    success = IntegerField(default=0)
    fail = IntegerField(default=0)
    empty = IntegerField(default=0)
    skip = IntegerField(default=0)
    captcha = IntegerField(default=0)
    start = IntegerField(default=0)
    elapsed = IntegerField(default=0)

    @staticmethod
    def get_account_stats(age_minutes=30):
        stats = {'working': 0, 'captcha': 0, 'failed': 0}
        timeout = datetime.datetime.utcnow() - datetime.timedelta(minutes=age_minutes)
        with MainWorker.database().execution_context():
            account_stats = (MainWorker
                             .select(fn.SUM(MainWorker.accounts_working),
                                     fn.SUM(MainWorker.accounts_captcha),
                                     fn.SUM(MainWorker.accounts_failed))
                             .where(MainWorker.last_modified >= timeout)
                             .scalar(as_tuple=True))
            if account_stats[0] is not None:
                stats.update({
                    'working': int(account_stats[0]),
                    'captcha': int(account_stats[1]),
                    'failed': int(account_stats[2])
                })
        return stats

    @staticmethod
    def get_recent(age_minutes=30):
        status = []
        timeout = datetime.datetime.utcnow() - datetime.timedelta(minutes=age_minutes)
        try:
            with MainWorker.database().execution_context():
                query = (MainWorker
                         .select()
                         .where(MainWorker.last_modified >= timeout)
                         .order_by(MainWorker.worker_name.asc())
                         .dicts())

                status = [dbmw for dbmw in query]
        except Exception as e:
            log.exception('Failed to retrieve main worker status: %s.', e)

        return status


class WorkerStatus(LatLongModel):
    username = Utf8mb4CharField(primary_key=True, max_length=50)
    worker_name = Utf8mb4CharField(index=True, max_length=50)
    success = IntegerField()
    fail = IntegerField()
    no_items = IntegerField()
    skip = IntegerField()
    captcha = IntegerField()
    last_modified = DateTimeField(index=True)
    message = Utf8mb4CharField(max_length=191)
    last_scan_date = DateTimeField(index=True)
    latitude = DoubleField(null=True)
    longitude = DoubleField(null=True)

    @staticmethod
    def db_format(status, name='status_worker_db'):
        status['worker_name'] = status.get('worker_name', name)
        return {'username': status['username'],
                'worker_name': status['worker_name'],
                'success': status['success'],
                'fail': status['fail'],
                'no_items': status['noitems'],
                'skip': status['skip'],
                'captcha': status['captcha'],
                'last_modified': datetime.datetime.utcnow(),
                'message': status['message'],
                'last_scan_date': status.get('last_scan_date',
                                             datetime.datetime.utcnow()),
                'latitude': status.get('latitude', None),
                'longitude': status.get('longitude', None)}

    @staticmethod
    def get_recent(age_minutes=30):
        status = []
        timeout = datetime.datetime.utcnow() - datetime.timedelta(minutes=age_minutes)
        try:
            with WorkerStatus.database().execution_context():
                query = (WorkerStatus
                         .select()
                         .where(WorkerStatus.last_modified >= timeout)
                         .order_by(WorkerStatus.username.asc())
                         .dicts())

                status = [dbws for dbws in query]
        except Exception as e:
            log.exception('Failed to retrieve worker status: %s.', e)

        return status

    @staticmethod
    def get_worker(username):
        res = None
        with WorkerStatus.database().execution_context():
            try:
                res = WorkerStatus.select().where(
                    WorkerStatus.username == username).dicts().get()
            except WorkerStatus.DoesNotExist:
                pass
        return res


class SpawnPoint(LatLongModel):
    id = Utf8mb4CharField(index=True, max_length=100, primary_key=True)
    latitude = DoubleField()
    longitude = DoubleField()
    last_scanned = DateTimeField(index=True)
    # kind gives the four quartiles of the spawn, as 's' for seen
    # or 'h' for hidden.  For example, a 30 minute spawn is 'hhss'.
    kind = Utf8mb4CharField(max_length=4, default='hhhs')

    # links shows whether a Pokemon encounter id changes between quartiles or
    # stays the same.  Both 1x45 and 1x60h3 have the kind of 'sssh', but the
    # different links shows when the encounter id changes.  Same encounter id
    # is shared between two quartiles, links shows a '+'.  A different
    # encounter id between two quartiles is a '-'.
    #
    # For the hidden times, an 'h' is used.  Until determined, '?' is used.
    # Note index is shifted by a half. links[0] is the link between
    # kind[0] and kind[1] and so on. links[3] is the link between
    # kind[3] and kind[0]
    links = Utf8mb4CharField(max_length=4, default='????')

    # Count consecutive times spawn should have been seen, but wasn't.
    # If too high, will not be scheduled for review, and treated as inactive.
    missed_count = IntegerField(default=0)

    # Next 2 fields are to narrow down on the valid TTH window.
    # Seconds after the hour of the latest Pokemon seen time within the hour.
    latest_seen = SmallIntegerField()

    # Seconds after the hour of the earliest time Pokemon wasn't seen after an
    # appearance.
    earliest_unseen = SmallIntegerField()

    class Meta:
        indexes = ((('latitude', 'longitude'), False),)
        constraints = [Check('earliest_unseen >= 0'),
                       Check('earliest_unseen <= 3600'),
                       Check('latest_seen >= 0'),
                       Check('latest_seen <= 3600')]

    # Returns the spawnpoint dict from ID, or a new dict if not found.
    @staticmethod
    def get_by_id(id, latitude=0, longitude=0):
        with SpawnPoint.database().execution_context():
            query = (SpawnPoint
                     .select()
                     .where(SpawnPoint.id == id)
                     .dicts())

            result = query[0] if query else {
                'id': id,
                'latitude': latitude,
                'longitude': longitude,
                'last_scanned': None,  # Null value used as new flag.
                'kind': 'hhhs',
                'links': '????',
                'missed_count': 0,
                'latest_seen': 0,
                'earliest_unseen': 0
            }
        return result

    @staticmethod
    def get_spawnpoints(swLat, swLng, neLat, neLng, timestamp=0,
                        oSwLat=None, oSwLng=None, oNeLat=None, oNeLng=None):
        spawnpoints = {}
        with SpawnPoint.database().execution_context():
            query = (SpawnPoint.select(
                SpawnPoint.latitude, SpawnPoint.longitude, SpawnPoint.id,
                SpawnPoint.links, SpawnPoint.kind, SpawnPoint.latest_seen,
                SpawnPoint.earliest_unseen, ScannedLocation.done)
                     .join(ScanSpawnPoint).join(ScannedLocation).dicts())

            if timestamp > 0:
                query = (
                    query.where(((SpawnPoint.last_scanned >
                                  datetime.datetime.utcfromtimestamp(timestamp / 1000)))
                                & ((SpawnPoint.latitude >= swLat) &
                                   (SpawnPoint.longitude >= swLng) &
                                   (SpawnPoint.latitude <= neLat) &
                                   (SpawnPoint.longitude <= neLng))).dicts())
            elif oSwLat and oSwLng and oNeLat and oNeLng:
                # Send spawnpoints in view but exclude those within old
                # boundaries. Only send newly uncovered spawnpoints.
                query = (query
                         .where((((SpawnPoint.latitude >= swLat) &
                                  (SpawnPoint.longitude >= swLng) &
                                  (SpawnPoint.latitude <= neLat) &
                                  (SpawnPoint.longitude <= neLng))) &
                                ~((SpawnPoint.latitude >= oSwLat) &
                                  (SpawnPoint.longitude >= oSwLng) &
                                  (SpawnPoint.latitude <= oNeLat) &
                                  (SpawnPoint.longitude <= oNeLng)))
                         .dicts())
            elif swLat and swLng and neLat and neLng:
                query = (query
                         .where((SpawnPoint.latitude <= neLat) &
                                (SpawnPoint.latitude >= swLat) &
                                (SpawnPoint.longitude >= swLng) &
                                (SpawnPoint.longitude <= neLng)))

            queryDict = query.dicts()
            for sp in queryDict:
                key = sp['id']
                appear_time, disappear_time = SpawnPoint.start_end(sp)
                spawnpoints[key] = sp
                spawnpoints[key]['disappear_time'] = disappear_time
                spawnpoints[key]['appear_time'] = appear_time
                if not SpawnPoint.tth_found(sp) or not sp['done']:
                    spawnpoints[key]['uncertain'] = True

        # Helping out the GC.
        for sp in spawnpoints.values():
            del sp['done']
            del sp['kind']
            del sp['links']
            del sp['latest_seen']
            del sp['earliest_unseen']

        if args.china:
            for result in spawnpoints.values():
                result['latitude'], result['longitude'] = \
                    transform_from_wgs_to_gcj(
                        result['latitude'], result['longitude'])

        return list(spawnpoints.values())

    @staticmethod
    def get_nearby_spawnpoints(lat, lng, dist, unknown_tth, maxpoints, geofence_name, scheduled_points, geofences):
        spawnpoints = {}
        with SpawnPoint.database().execution_context():
            query = (SpawnPoint.select(
                SpawnPoint.latitude, SpawnPoint.longitude, SpawnPoint.id,
                SpawnPoint.latest_seen, SpawnPoint.earliest_unseen)
                .dicts())

            lat1 = lat - 0.1
            lat2 = lat + 0.1
            lng1 = lng - 0.1
            lng2 = lng + 0.1

            if geofence_name != "":
                lat1, lng1, lat2, lng2 = geofences.get_boundary_coords(geofence_name)

            minlat = min(lat1, lat2)
            maxlat = max(lat1, lat2)
            minlng = min(lng1, lng2)
            maxlng = max(lng1, lng2)

            if dist > 0:
                query = (query
                         .where((((SpawnPoint.latitude >= minlat) &
                                  (SpawnPoint.longitude >= minlng) &
                                  (SpawnPoint.latitude <= maxlat) &
                                  (SpawnPoint.longitude <= maxlng))))
                         .dicts())

            if len(scheduled_points) > 0:
                query = (query
                         .where(SpawnPoint.id.not_in(scheduled_points))
                         .dicts())

            queryDict = query.dicts()

            if len(queryDict) > 0 and geofences.is_enabled():
                queryDict = geofences.get_geofenced_results(queryDict, geofence_name)

            for sp in queryDict:
                key = sp['id']
                latitude = round(sp['latitude'], 5)
                longitude = round(sp['longitude'], 5)
                distance = geopy.distance.vincenty((lat, lng), (latitude, longitude)).km
                if (not unknown_tth or SpawnPoint.tth_found(sp)) and (dist == 0 or distance <= dist):
                    spawnpoints[key] = {
                        'latitude': latitude,
                        'longitude': longitude,
                        'distance': distance,
                        'key': key
                    }
            orderedspawnpoints = OrderedDict(sorted(spawnpoints.items(), key=lambda x: x[1]['distance']))

            maxlength = len(orderedspawnpoints)
            if (not isinstance(unknown_tth, (bool)) and maxlength > unknown_tth):
                maxlength = unknown_tth

            if (not isinstance(maxpoints, (bool)) and maxlength > maxpoints):
                maxlength = maxpoints

            while len(orderedspawnpoints) > maxlength:
                orderedspawnpoints.popitem()

            result = []
            while len(orderedspawnpoints) > 0:
                value = list(orderedspawnpoints.items())[0][1]
                result.append((value['latitude'], value['longitude'], value['key']))
                newlat = value['latitude']
                newlong = value['longitude']
                orderedspawnpoints.popitem(last=False)
                orderedspawnpoints = OrderedDict(sorted(orderedspawnpoints.items(), key=lambda x: geopy.distance.vincenty((newlat, newlong), (x[1]['latitude'], x[1]['longitude'])).km))

        return result

    # Confirm if TTH has been found.
    @staticmethod
    def tth_found(sp):
        # Fully identified if no '?' in links and
        # latest_seen % 3600 == earliest_unseen % 3600.
        # Warning: python uses modulo as the least residue, not as
        # remainder, so we don't apply it to the result.
        latest_seen = (sp['latest_seen'] % 3600)
        earliest_unseen = (sp['earliest_unseen'] % 3600)

        # If earliest_unseen and latest_seen are both 0 we cannot assume tth has been
        # found since that is what they are defaulted to upon creation of a new sp
        if latest_seen == 0 and earliest_unseen == 0:
            return False

        return latest_seen - earliest_unseen == 0

    # Return [start, end] in seconds after the hour for the spawn, despawn
    # time of a spawnpoint.
    @staticmethod
    def start_end(sp, spawn_delay=0, links=False):
        links_arg = links
        links = links if links else str(sp['links'])

        if links == '????':  # Clean up for old data.
            links = str(sp['kind'].replace('s', '?'))

        # Make some assumptions if link not fully identified.
        if links.count('-') == 0:
            links = links[:-1] + '-'

        links = links.replace('?', '+')

        links = links[:-1] + '-'
        plus_or_minus = links.index('+') if links.count('+') else links.index(
            '-')
        start = sp['earliest_unseen'] - (4 - plus_or_minus) * 900 + spawn_delay
        no_tth_adjust = 60 if not links_arg and not SpawnPoint.tth_found(
            sp) else 0
        end = sp['latest_seen'] - (3 - links.index('-')) * 900 + no_tth_adjust
        return [start % 3600, end % 3600]

    # Return a list of dicts with the next spawn times.
    @staticmethod
    def get_times(cell, scan, now_date, scan_delay,
                  cell_to_linked_spawn_points, sp_by_id):
        result = []
        now_secs = date_secs(now_date)
        linked_spawn_points = (cell_to_linked_spawn_points[cell]
                               if cell in cell_to_linked_spawn_points else [])

        for sp in linked_spawn_points:

            if sp['missed_count'] > 5:
                continue

            endpoints = SpawnPoint.start_end(sp, scan_delay)
            SpawnPoint.add_if_not_scanned('spawn', result, sp, scan,
                                          endpoints[0], endpoints[1], now_date,
                                          now_secs, sp_by_id)

            # Check to see if still searching for valid TTH.
            if SpawnPoint.tth_found(sp):
                continue

            # Add a spawnpoint check between latest_seen and earliest_unseen.
            start = sp['latest_seen']
            end = sp['earliest_unseen']

            # So if the gap between start and end < 89 seconds make the gap
            # 89 seconds
            if ((end > start and end - start < 89) or
                    (start > end and (end + 3600) - start < 89)):
                end = (start + 89) % 3600
            # So we move the search gap on 45 to within 45 and 89 seconds from
            # the last scan. TTH appears in the last 90 seconds of the Spawn.
            start = sp['latest_seen'] + 45

            SpawnPoint.add_if_not_scanned('TTH', result, sp, scan, start, end,
                                          now_date, now_secs, sp_by_id)

        return result

    @staticmethod
    def add_if_not_scanned(kind, l, sp, scan, start,
                           end, now_date, now_secs, sp_by_id):
        # Make sure later than now_secs.
        while end < now_secs:
            start, end = start + 3600, end + 3600

        # Ensure start before end.
        while start > end:
            start -= 3600

        while start < 0:
            start, end = start + 3600, end + 3600

        last_scanned = sp_by_id[sp['id']]['last_scanned']
        if ((now_date - last_scanned).total_seconds() > now_secs - start):
            l.append(ScannedLocation._q_init(scan, start, end, kind, sp['id']))

    @staticmethod
    def select_in_hex_by_cellids(cellids, location_change_date):
        # Get all spawnpoints from the hive's cells
        sp_from_cells = (ScanSpawnPoint
                         .select(ScanSpawnPoint.spawnpoint)
                         .where(ScanSpawnPoint.scannedlocation << cellids)
                         .alias('spcells'))
        # Allocate a spawnpoint to one cell only, this can either be
        # A new SL (new ones are created when the location changes) or
        # it can be a cell from another active hive
        one_sp_scan = (ScanSpawnPoint
                       .select(ScanSpawnPoint.spawnpoint,
                               fn.MAX(ScanSpawnPoint.scannedlocation).alias(
                                   'Max_ScannedLocation_id'))
                       .join(sp_from_cells, on=sp_from_cells.c.spawnpoint_id
                             == ScanSpawnPoint.spawnpoint)
                       .join(
                           ScannedLocation,
                           on=(ScannedLocation.cellid
                               == ScanSpawnPoint.scannedlocation))
                       .where(((ScannedLocation.last_modified
                                >= (location_change_date)) & (
                           ScannedLocation.last_modified > (
                               datetime.datetime.utcnow() - datetime.timedelta(minutes=60)))) |
                              (ScannedLocation.cellid << cellids))
                       .group_by(ScanSpawnPoint.spawnpoint)
                       .alias('maxscan'))

        in_hex = []
        with SpawnPoint.database().execution_context():
            query = (SpawnPoint
                     .select(SpawnPoint)
                     .join(one_sp_scan,
                           on=(one_sp_scan.c.spawnpoint_id == SpawnPoint.id))
                     .where(one_sp_scan.c.Max_ScannedLocation_id << cellids)
                     .dicts())

            for spawn in list(query):
                in_hex.append(spawn)
        return in_hex

    @staticmethod
    def select_in_hex_by_location(center, steps):
        R = 6378.1  # KM radius of the earth
        hdist = ((steps * 120.0) - 50.0) / 1000.0
        n, e, s, w = hex_bounds(center, steps)

        in_hex = []
        # Get all spawns in that box.
        with SpawnPoint.database().execution_context():
            sp = list(SpawnPoint
                      .select()
                      .where((SpawnPoint.latitude <= n) &
                             (SpawnPoint.latitude >= s) &
                             (SpawnPoint.longitude >= w) &
                             (SpawnPoint.longitude <= e))
                      .dicts())

            # For each spawn work out if it is in the hex
            # (clipping the diagonals).
            for spawn in sp:
                # Get the offset from the center of each spawn in km.
                offset = [math.radians(spawn['latitude'] - center[0]) * R,
                          math.radians(spawn['longitude'] - center[1]) *
                          (R * math.cos(math.radians(center[0])))]
                # Check against the 4 lines that make up the diagonals.
                if (offset[1] + (offset[0] * 0.5)) > hdist:  # Too far NE
                    continue
                if (offset[1] - (offset[0] * 0.5)) > hdist:  # Too far SE
                    continue
                if ((offset[0] * 0.5) - offset[1]) > hdist:  # Too far NW
                    continue
                if ((0 - offset[1]) - (offset[0] * 0.5)) > hdist:  # Too far SW
                    continue
                # If it gets to here it's a good spawn.
                in_hex.append(spawn)
        return in_hex


class ScanSpawnPoint(BaseModel):
    scannedlocation = ForeignKeyField(ScannedLocation, null=True)
    spawnpoint = ForeignKeyField(SpawnPoint, null=True)

    class Meta:
        primary_key = CompositeKey('spawnpoint', 'scannedlocation')


class SpawnpointDetectionData(BaseModel):
    id = PrimaryKeyField()
    # Removed ForeignKeyField since it caused MySQL issues.
    encounter_id = UBigIntegerField()
    # Removed ForeignKeyField since it caused MySQL issues.
    spawnpoint_id = Utf8mb4CharField(index=True, max_length=100)
    scan_time = DateTimeField()
    tth_secs = SmallIntegerField(null=True)

    @staticmethod
    def set_default_earliest_unseen(sp):
        sp['earliest_unseen'] = (sp['latest_seen'] + 15 * 60) % 3600

    @staticmethod
    def classify(sp, scan_loc, now_secs, sighting=None):

        # Get past sightings.
        with SpawnpointDetectionData.database().execution_context():
            query = list(
                SpawnpointDetectionData.select()
                .where(SpawnpointDetectionData.spawnpoint_id == sp['id'])
                .order_by(SpawnpointDetectionData.scan_time.asc()).dicts())

        if sighting:
            query.append(sighting)

        tth_found = False
        for s in query:
            if s['tth_secs'] is not None:
                tth_found = True
                tth_secs = (s['tth_secs'] - 1) % 3600

        # To reduce CPU usage, give an intial reading of 15 minute spawns if
        # not done with initial scan of location.
        if not scan_loc['done']:
            # We only want to reset a SP if it is new and not due the
            # location changing (which creates new Scannedlocations)
            if not tth_found:
                sp['kind'] = 'hhhs'
                if not sp['earliest_unseen']:
                    sp['latest_seen'] = now_secs
                    SpawnpointDetectionData.set_default_earliest_unseen(sp)

                elif clock_between(sp['latest_seen'], now_secs,
                                   sp['earliest_unseen']):
                    sp['latest_seen'] = now_secs
            return

        # Make a record of links, so we can reset earliest_unseen
        # if it changes.
        old_kind = str(sp['kind'])
        # Make a sorted list of the seconds after the hour.
        seen_secs = sorted(map(lambda x: date_secs(x['scan_time']), query))
        # Include and entry for the TTH if it found
        if tth_found:
            seen_secs.append(tth_secs)
            seen_secs.sort()
        # Add the first seen_secs to the end as a clock wrap around.
        if seen_secs:
            seen_secs.append(seen_secs[0] + 3600)

        # Make a list of gaps between sightings.
        gap_list = [seen_secs[i + 1] - seen_secs[i]
                    for i in range(len(seen_secs) - 1)]

        max_gap = max(gap_list)

        # An hour minus the largest gap in minutes gives us the duration the
        # spawn was there.  Round up to the nearest 15 minute interval for our
        # current best guess duration.
        duration = (int((60 - max_gap / 60.0) / 15) + 1) * 15

        # If the second largest gap is larger than 15 minutes, then there are
        # two gaps greater than 15 minutes.  It must be a double spawn.
        if len(gap_list) > 4 and sorted(gap_list)[-2] > 900:
            sp['kind'] = 'hshs'
            sp['links'] = 'h?h?'

        else:
            # Convert the duration into a 'hhhs', 'hhss', 'hsss', 'ssss' string
            # accordingly.  's' is for seen, 'h' is for hidden.
            sp['kind'] = ''.join(
                ['s' if i > (3 - duration / 15) else 'h' for i in range(0, 4)])

        # Assume no hidden times.
        sp['links'] = sp['kind'].replace('s', '?')

        if sp['kind'] != 'ssss':
            # Cover all bases, make sure we're using values < 3600.
            # Warning: python uses modulo as the least residue, not as
            # remainder, so we don't apply it to the result.
            residue_unseen = sp['earliest_unseen'] % 3600
            residue_seen = sp['latest_seen'] % 3600
            if (not sp['earliest_unseen'] or
                    residue_unseen != residue_seen or
                    not tth_found):

                # New latest_seen will be just before max_gap.
                sp['latest_seen'] = seen_secs[gap_list.index(max_gap)]

                # if we don't have a earliest_unseen yet or if the kind of
                # spawn has changed, reset to latest_seen + 14 minutes.
                if not sp['earliest_unseen'] or sp['kind'] != old_kind:
                    SpawnpointDetectionData.set_default_earliest_unseen(sp)
            return

        # Only ssss spawns from here below.

        sp['links'] = '+++-'

        # Cover all bases, make sure we're using values < 3600.
        # Warning: python uses modulo as the least residue, not as
        # remainder, so we don't apply it to the result.
        residue_unseen = sp['earliest_unseen'] % 3600
        residue_seen = sp['latest_seen'] % 3600

        if residue_unseen == residue_seen:
            return

        # Make a sight_list of dicts:
        # {date: first seen time,
        # delta: duration of sighting,
        # same: whether encounter ID was same or different over that time}
        #
        # For 60 minute spawns ('ssss'), the largest gap doesn't give the
        # earliest spawnpoint because a Pokemon is always there.  Use the union
        # of all intervals where the same encounter ID was seen to find the
        # latest_seen.  If a different encounter ID was seen, then the
        # complement of that interval was the same ID, so union that
        # complement as well.

        sight_list = [{'date': query[i]['scan_time'],
                       'delta': query[i + 1]['scan_time'] -
                       query[i]['scan_time'],
                       'same': query[i + 1]['encounter_id'] ==
                       query[i]['encounter_id']
                       }
                      for i in range(len(query) - 1)
                      if query[i + 1]['scan_time'] - query[i]['scan_time'] <
                      datetime.timedelta(hours=1)
                      ]

        start_end_list = []
        for s in sight_list:
            if s['same']:
                # Get the seconds past the hour for start and end times.
                start = date_secs(s['date'])
                end = (start + int(s['delta'].total_seconds())) % 3600

            else:
                # Convert diff range to same range by taking the clock
                # complement.
                start = date_secs(s['date'] + s['delta']) % 3600
                end = date_secs(s['date'])

            start_end_list.append([start, end])

        # Take the union of all the ranges.
        while True:
            # union is list of unions of ranges with the same encounter id.
            union = []
            for start, end in start_end_list:
                if not union:
                    union.append([start, end])
                    continue
                # Cycle through all ranges in union, since it might overlap
                # with any of them.
                for u in union:
                    if clock_between(u[0], start, u[1]):
                        u[1] = end if not(clock_between(
                            u[0], end, u[1])) else u[1]
                    elif clock_between(u[0], end, u[1]):
                        u[0] = start if not(clock_between(
                            u[0], start, u[1])) else u[0]
                    elif union.count([start, end]) == 0:
                        union.append([start, end])

            # Are no more unions possible?
            if union == start_end_list:
                break

            start_end_list = union  # Make another pass looking for unions.

        # If more than one disparate union, take the largest as our starting
        # point.
        union = reduce(lambda x, y: x if (x[1] - x[0]) % 3600 >
                       (y[1] - y[0]) % 3600 else y, union, [0, 3600])
        sp['latest_seen'] = union[1]
        sp['earliest_unseen'] = union[0]
        log.info('1x60: appear %d, despawn %d, duration: %d min.',
                 union[0], union[1], ((union[1] - union[0]) % 3600) / 60)

    # Expand the seen times for 30 minute spawnpoints based on scans when spawn
    # wasn't there.  Return true if spawnpoint dict changed.
    @staticmethod
    def unseen(sp, now_secs):

        # Return if we already have a tth.
        # Cover all bases, make sure we're using values < 3600.
        # Warning: python uses modulo as the least residue, not as
        # remainder, so we don't apply it to the result.
        residue_unseen = sp['earliest_unseen'] % 3600
        residue_seen = sp['latest_seen'] % 3600

        if residue_seen == residue_unseen:
            return False

        # If now_secs is later than the latest seen return.
        if not clock_between(sp['latest_seen'], now_secs,
                             sp['earliest_unseen']):
            return False

        sp['earliest_unseen'] = now_secs

        return True


class Versions(BaseModel):
    key = Utf8mb4CharField()
    val = SmallIntegerField()

    class Meta:
        primary_key = False


class GymMember(BaseModel):
    gym_id = Utf8mb4CharField(index=True)
    pokemon_uid = UBigIntegerField(index=True)
    last_scanned = DateTimeField(default=datetime.datetime.utcnow, index=True)
    deployment_time = DateTimeField()
    cp_decayed = SmallIntegerField()

    class Meta:
        primary_key = False


class PokestopMember(BaseModel):
    encounter_id = UBigIntegerField(primary_key=True)
    pokestop_id = Utf8mb4CharField(index=True, max_length=100)
    pokemon_id = SmallIntegerField(index=True)
    disappear_time = DateTimeField()
    gender = SmallIntegerField(null=True)
    costume = SmallIntegerField(null=True)
    form = SmallIntegerField(null=True)
    weather_boosted_condition = SmallIntegerField(null=True)
    last_modified = DateTimeField(
        null=True, index=True, default=datetime.datetime.utcnow)
    distance = DoubleField()


class GymPokemon(BaseModel):
    pokemon_uid = UBigIntegerField(primary_key=True)
    pokemon_id = SmallIntegerField()
    cp = SmallIntegerField()
    num_upgrades = SmallIntegerField(null=True)
    move_1 = SmallIntegerField(null=True)
    move_2 = SmallIntegerField(null=True)
    height = FloatField(null=True)
    weight = FloatField(null=True)
    stamina = SmallIntegerField(null=True)
    stamina_max = SmallIntegerField(null=True)
    cp_multiplier = FloatField(null=True)
    additional_cp_multiplier = FloatField(null=True)
    iv_defense = SmallIntegerField(null=True)
    iv_stamina = SmallIntegerField(null=True)
    iv_attack = SmallIntegerField(null=True)
    costume = SmallIntegerField(null=True)
    form = SmallIntegerField(null=True)
    shiny = SmallIntegerField(null=True)
    last_seen = DateTimeField(default=datetime.datetime.utcnow)


class GymDetails(BaseModel):
    gym_id = Utf8mb4CharField(primary_key=True, max_length=50)
    name = Utf8mb4CharField()
    description = TextField(null=True, default="")
    url = Utf8mb4CharField()
    last_scanned = DateTimeField(default=datetime.datetime.utcnow)


class PokestopDetails(BaseModel):
    pokestop_id = Utf8mb4CharField(primary_key=True, max_length=50)
    name = Utf8mb4CharField()
    description = TextField(null=True, default="")
    url = Utf8mb4CharField()
    last_scanned = DateTimeField(default=datetime.datetime.utcnow)


class Token(BaseModel):
    token = TextField()
    last_updated = DateTimeField(default=datetime.datetime.utcnow, index=True)

    @staticmethod
    def get_valid(limit=15):
        # Make sure we don't grab more than we can process
        if limit > 15:
            limit = 15
        valid_time = datetime.datetime.utcnow() - datetime.timedelta(seconds=30)
        token_ids = []
        tokens = []
        try:
            with Token.database().execution_context():
                query = (Token
                         .select()
                         .where(Token.last_updated > valid_time)
                         .order_by(Token.last_updated.asc())
                         .limit(limit)
                         .dicts())
                for t in query:
                    token_ids.append(t['id'])
                    tokens.append(t['token'])
                if tokens:
                    log.debug('Retrieved Token IDs: %s.', token_ids)
                    query = DeleteQuery(Token).where(Token.id << token_ids)
                    rows = query.execute()
                    log.debug('Claimed and removed %d captcha tokens.', rows)
        except OperationalError as e:
            log.exception('Failed captcha token transactional query: %s.', e)

        return tokens

class Weather(BaseModel):
    s2_cell_id = Utf8mb4CharField(primary_key=True, max_length=50)
    latitude = DoubleField()
    longitude = DoubleField()
    cloud_level = SmallIntegerField(null=True, index=True, default=0)
    rain_level = SmallIntegerField(null=True, index=True, default=0)
    wind_level = SmallIntegerField(null=True, index=True, default=0)
    snow_level = SmallIntegerField(null=True, index=True, default=0)
    fog_level = SmallIntegerField(null=True, index=True, default=0)
    wind_direction = SmallIntegerField(null=True, index=True, default=0)
    gameplay_weather = SmallIntegerField(null=True, index=True, default=0)
    severity = SmallIntegerField(null=True, index=True, default=0)
    warn_weather = SmallIntegerField(null=True, index=True, default=0)
    time_of_day = SmallIntegerField(null=True, index=True, default=0)
    last_updated = DateTimeField(default=datetime.datetime.utcnow, null=True, index=True)


    @staticmethod
    def get_weathers():
        weathers = []
        with Weather.database().execution_context():
            query = Weather.select().dicts()
            for w in query:
                weathers.append(w)

        return weathers

    @staticmethod
    def get_weather_by_location(swLat, swLng, neLat, neLng, alert):
        # We can filter by the center of a cell, this deltas can expand the viewport bounds
        # So cells with center outside the viewport, but close to it can be rendered
        # otherwise edges of cells that intersects with viewport won't be rendered
        lat_delta = 0.15
        lng_delta = 0.4
        weathers = []
        with Weather.database().execution_context():
            if not alert:
                query = Weather.select().where((Weather.latitude >= float(swLat) - lat_delta) &
                                               (Weather.longitude >= float(swLng) - lng_delta) &
                                               (Weather.latitude <= float(neLat) + lat_delta) &
                                               (Weather.longitude <= float(neLng) + lng_delta)).dicts()
            else:
                query = Weather.select().where((Weather.latitude >= float(swLat) - lat_delta) &
                                               (Weather.longitude >= float(swLng) - lng_delta) &
                                               (Weather.latitude <= float(neLat) + lat_delta) &
                                               (Weather.longitude <= float(neLng) + lng_delta) &
                                               (Weather.severity.is_null(False))).dicts()
            for w in query:
                weathers.append(w)

        return weathers

class HashKeys(BaseModel):
    key = Utf8mb4CharField(primary_key=True, max_length=20)
    maximum = IntegerField(default=0)
    remaining = IntegerField(default=0)
    peak = IntegerField(default=0)
    expires = DateTimeField(null=True)
    last_updated = DateTimeField(default=datetime.datetime.utcnow)

    # Obfuscate hashing keys before sending them to the front-end.
    @staticmethod
    def get_obfuscated_keys():
        hashkeys = HashKeys.get_all()
        for i, s in enumerate(hashkeys):
            hashkeys[i]['key'] = s['key'][:-9] + '*'*9
        return hashkeys

    # Retrieve stored 'peak' value from recently used hashing keys.
    @staticmethod
    def get_stored_peaks():
        hashkeys = {}
        try:
            with HashKeys.database().execution_context():
                query = (HashKeys
                         .select(HashKeys.key, HashKeys.peak)
                         .where(HashKeys.last_updated >
                                (datetime.datetime.utcnow() - datetime.timedelta(minutes=30)))
                         .dicts())
                for dbhk in query:
                    hashkeys[dbhk['key']] = dbhk['peak']
        except OperationalError as e:
            log.exception('Failed to get hashing keys stored peaks: %s.', e)

        return hashkeys


def hex_bounds(center, steps=None, radius=None):
    # Make a box that is (70m * step_limit * 2) + 70m away from the
    # center point.  Rationale is that you need to travel.
    sp_dist = 0.07 * (2 * steps + 1) if steps else radius
    n = get_new_coords(center, sp_dist, 0)[0]
    e = get_new_coords(center, sp_dist, 90)[1]
    s = get_new_coords(center, sp_dist, 180)[0]
    w = get_new_coords(center, sp_dist, 270)[1]
    return (n, e, s, w)


# todo: this probably shouldn't _really_ be in "models" anymore, but w/e.
def parse_map(args, map_dict, scan_coords, scan_location, db_update_queue,
              wh_update_queue, key_scheduler, api, status, now_date, account,
              account_sets):
    pokemon = {}
    pokestops = {}
    gyms = {}
    raids = {}
    skipped = 0
    filtered = 0
    stopsskipped = 0
    forts = []
    forts_count = 0
    wild_pokemon = []
    wild_pokemon_count = 0
    nearby_pokemon = 0
    spawn_points = {}
    scan_spawn_points = {}
    sightings = {}
    new_spawn_points = []
    sp_id_list = []

    # Consolidate the individual lists in each cell into two lists of Pokemon
    # and a list of forts.
    cells = map_dict['responses']['GET_MAP_OBJECTS'].map_cells
    # Get the level for the pokestop spin, and to send to webhook.
    level = account['level']
    # Use separate level indicator for our L30 encounters.
    encounter_level = level

    for i, cell in enumerate(cells):
        # If we have map responses then use the time from the request
        if i == 0:
            now_date = datetime.datetime.utcfromtimestamp(
                cell.current_timestamp_ms / 1000)

        nearby_pokemon += len(cell.nearby_pokemons)
        # Parse everything for stats (counts).  Future enhancement -- we don't
        # necessarily need to know *how many* forts/wild/nearby were found but
        # we'd like to know whether or not *any* were found to help determine
        # if a scan was actually bad.
        if not args.no_pokemon:
            wild_pokemon += cell.wild_pokemons

        if not args.no_pokestops or not args.no_gyms:
            forts += cell.forts

        wild_pokemon_count += len(cell.wild_pokemons)
        forts_count += len(cell.forts)

    now_secs = date_secs(now_date)

    del map_dict['responses']['GET_MAP_OBJECTS']

    # If there are no wild or nearby Pokemon...
    if not wild_pokemon and not nearby_pokemon:
        # ...and there are no gyms/pokestops then it's unusable/bad.
        if not forts:
            log.warning('Bad scan. Parsing found absolutely nothing'
                        + ' using account %s.', account['username'])
            log.info('Common causes: captchas or IP bans.')
        elif not args.no_pokemon:
            # When gym scanning we'll go over the speed limit
            # and Pokémon will be invisible, but we'll still be able
            # to scan gyms so we disable the error logging.
            # No wild or nearby Pokemon but there are forts. It's probably
            # a speed violation.
            log.warning('No nearby or wild Pokemon but there are visible '
                        'gyms or pokestops. Possible speed violation.')

    done_already = scan_location['done']
    ScannedLocation.update_band(scan_location, now_date)
    just_completed = not done_already and scan_location['done']

    if wild_pokemon and not args.no_pokemon:
        encounter_ids = [p.encounter_id for p in wild_pokemon]
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
                (p['encounter_id'], p['spawnpoint_id']) for p in query]

        for p in wild_pokemon:
            spawn_id = int(p.spawn_point_id, 16)
            sp = SpawnPoint.get_by_id(spawn_id, p.latitude,
                                      p.longitude)
            spawn_points[spawn_id] = sp
            sp['missed_count'] = 0

            sighting = {
                'encounter_id': p.encounter_id,
                'spawnpoint_id': spawn_id,
                'scan_time': now_date,
                'tth_secs': None
            }

            # Keep a list of sp_ids to return.
            sp_id_list.append(spawn_id)

            # time_till_hidden_ms was overflowing causing a negative integer.
            # It was also returning a value above 3.6M ms.
            if 0 < p.time_till_hidden_ms < 3600000:
                d_t_secs = date_secs(datetime.datetime.utcfromtimestamp(
                    (p.last_modified_timestamp_ms +
                     p.time_till_hidden_ms) / 1000.0))

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

            scan_spawn_points[len(scan_spawn_points)+1] = {
                'spawnpoint': sp['id'],
                'scannedlocation': scan_location['cellid']}
            if not sp['last_scanned']:
                log.info('New Spawn Point found.')
                new_spawn_points.append(sp)

                # If we found a new spawnpoint after the location was already
                # fully scanned then either it's new, or we had a bad scan.
                # Either way, rescan the location.
                if scan_location['done'] and not just_completed:
                    log.warning('Location was fully scanned, and yet a brand '
                                'new spawnpoint found.')
                    log.warning('Redoing scan of this location to identify '
                                'new spawnpoint.')
                    ScannedLocation.reset_bands(scan_location)

            if (not SpawnPoint.tth_found(sp) or sighting['tth_secs'] or
                    not scan_location['done'] or just_completed):
                SpawnpointDetectionData.classify(sp, scan_location, now_secs,
                                                 sighting)
                sightings[p.encounter_id] = sighting

            sp['last_scanned'] = datetime.datetime.utcfromtimestamp(
                p.last_modified_timestamp_ms / 1000.0)

            if ((p.encounter_id, spawn_id) in encountered_pokemon):
                # If Pokemon has been encountered before don't process it.
                skipped += 1
                continue

            start_end = SpawnPoint.start_end(sp, 1)
            seconds_until_despawn = (start_end[1] - now_secs) % 3600
            disappear_time = now_date + \
                datetime.timedelta(seconds=seconds_until_despawn)

            pokemon_id = p.pokemon_data.pokemon_id

            # If this is an ignored pokemon, skip this whole section.
            # We want the stuff above or we will impact spawn detection
            # but we don't want to insert it, or send it to webhooks.
            if args.ignorelist_file and pokemon_id in args.ignorelist:
                log.debug('Ignoring Pokemon id: %i.', pokemon_id)
                filtered += 1
                continue

            printPokemon(pokemon_id, p.latitude, p.longitude,
                         disappear_time)

            # Scan for IVs/CP and moves.
            pokemon_info = False
            if args.encounter and (pokemon_id in args.enc_whitelist):
                pokemon_info = encounter_pokemon(
                    args, p, account, api, account_sets, status, key_scheduler)

            pokemon[p.encounter_id] = {
                'encounter_id': p.encounter_id,
                'spawnpoint_id': spawn_id,
                'pokemon_id': pokemon_id,
                'latitude': p.latitude,
                'longitude': p.longitude,
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
                'gender': p.pokemon_data.pokemon_display.gender,
                'costume': p.pokemon_data.pokemon_display.costume,
                'form': p.pokemon_data.pokemon_display.form,
                'weather_boosted_condition': None

            }

            # Store Pokémon boosted condition.
            # TODO: Move pokemon_display to the top.
            pokemon_display = p.pokemon_data.pokemon_display
            boosted = pokemon_display.weather_boosted_condition
            if boosted:
                pokemon[p.encounter_id]['weather_boosted_condition'] = boosted

            # We need to check if exist and is not false due to a
            # request error.
            if pokemon_info:
                pokemon[p.encounter_id].update({
                    'individual_attack': pokemon_info.individual_attack,
                    'individual_defense': pokemon_info.individual_defense,
                    'individual_stamina': pokemon_info.individual_stamina,
                    'move_1': pokemon_info.move_1,
                    'move_2': pokemon_info.move_2,
                    'height': pokemon_info.height_m,
                    'weight': pokemon_info.weight_kg,
                    'cp': pokemon_info.cp,
                    'cp_multiplier': pokemon_info.cp_multiplier,
                    'gender': pokemon_info.pokemon_display.gender
                })

            if 'pokemon' in args.wh_types:
                if (pokemon_id in args.webhook_whitelist or
                    (not args.webhook_whitelist and pokemon_id
                     not in args.webhook_blacklist)):
                    wh_poke = pokemon[p.encounter_id].copy()
                    wh_poke.update({
                        'disappear_time': calendar.timegm(
                            disappear_time.timetuple()),
                        'last_modified_time': p.last_modified_timestamp_ms,
                        'time_until_hidden_ms': p.time_till_hidden_ms,
                        'verified': SpawnPoint.tth_found(sp),
                        'seconds_until_despawn': seconds_until_despawn,
                        'spawn_start': start_end[0],
                        'spawn_end': start_end[1],
                        'player_level': encounter_level
                    })
                    if wh_poke['cp_multiplier'] is not None:
                        wh_poke.update({
                            'pokemon_level': calc_pokemon_level(
                                wh_poke['cp_multiplier'])
                        })
                    wh_update_queue.put(('pokemon', wh_poke))

    if forts and (not args.no_pokestops or not args.no_gyms):
        if not args.no_pokestops:
            stop_ids = [f.id for f in forts if f.type == 1]
            if stop_ids:
                with Pokemon.database().execution_context():
                    query = (Pokestop.select(
                        Pokestop.pokestop_id, Pokestop.last_modified).where(
                            (Pokestop.pokestop_id << stop_ids)).dicts())
                    encountered_pokestops = [(f['pokestop_id'], int(
                        (f['last_modified'] - datetime.datetime(1970, 1,
                                                       1)).total_seconds()))
                                             for f in query]

        for f in forts:
            if not args.no_pokestops and f.type == 1:  # Pokestops.
                if len(f.active_fort_modifier) > 0:
                    lure_expiration = (datetime.datetime.utcfromtimestamp(
                        f.last_modified_timestamp_ms / 1000.0) +
                        datetime.timedelta(minutes=args.lure_duration))
                    active_fort_modifier = f.active_fort_modifier[0]
                else:
                    lure_expiration, active_fort_modifier = None, None

                if ((f.id, int(f.last_modified_timestamp_ms / 1000.0))
                        in encountered_pokestops):
                    # If pokestop has been encountered before and hasn't
                    # changed don't process it.
                    stopsskipped += 1
                    continue
                pokestops[f.id] = {
                    'pokestop_id': f.id,
                    'enabled': f.enabled,
                    'latitude': f.latitude,
                    'longitude': f.longitude,
                    'last_modified': datetime.datetime.utcfromtimestamp(
                        f.last_modified_timestamp_ms / 1000.0),
                    'lure_expiration': lure_expiration,
                    'active_fort_modifier': active_fort_modifier
                }

                # Send all pokestops to webhooks.
                if 'pokestop' in args.wh_types or (
                        'lure' in args.wh_types and
                        lure_expiration is not None):
                    l_e = None
                    if lure_expiration is not None:
                        l_e = calendar.timegm(lure_expiration.timetuple())
                    wh_pokestop = pokestops[f.id].copy()
                    wh_pokestop.update({
                        'pokestop_id': f.id,
                        'last_modified': f.last_modified_timestamp_ms,
                        'lure_expiration': l_e,
                    })
                    wh_update_queue.put(('pokestop', wh_pokestop))

            # Currently, there are only stops and gyms.
            elif not args.no_gyms and f.type == 0:
                b64_gym_id = str(f.id)
                gym_display = f.gym_display
                raid_info = f.raid_info
                park = Gym.get_gyms_park(f.id)

                # Send gyms to webhooks.

                if 'gym' in args.wh_types:
                    raid_active_until = 0
                    raid_battle_ms = raid_info.raid_battle_ms
                    raid_end_ms = raid_info.raid_end_ms

                    if raid_battle_ms / 1000 > time.time():
                        raid_active_until = raid_end_ms / 1000

                    # Explicitly set 'webhook_data', in case we want to change
                    # the information pushed to webhooks.  Similar to above
                    # and previous commits.
                    wh_update_queue.put(('gym', {
                        'gym_id':
                            b64_gym_id,
                        'team_id':
                            f.owned_by_team,
                        'park':
                            park,
                        'guard_pokemon_id':
                            f.guard_pokemon_id,
                        'slots_available':
                            gym_display.slots_available,
                        'total_cp':
                            gym_display.total_gym_cp,
                        'enabled':
                            f.enabled,
                        'latitude':
                            f.latitude,
                        'longitude':
                            f.longitude,
                        'lowest_pokemon_motivation':
                            gym_display.lowest_pokemon_motivation,
                        'occupied_since':
                            calendar.timegm((datetime.datetime.utcnow() - datetime.timedelta(
                                milliseconds=gym_display.occupied_millis)
                                            ).timetuple()),
                        'last_modified':
                            f.last_modified_timestamp_ms,
                        'raid_active_until':
                            raid_active_until
                    }))
                gyms[f.id] = {
                    'gym_id':
                        f.id,
                    'team_id':
                        f.owned_by_team,
                    'park':
                        park,
                    'guard_pokemon_id':
                        f.guard_pokemon_id,
                    'slots_available':
                        gym_display.slots_available,
                    'total_cp':
                        gym_display.total_gym_cp,
                    'enabled':
                        f.enabled,
                    'latitude':
                        f.latitude,
                    'longitude':
                        f.longitude,
                    'last_modified':
                        datetime.datetime.utcfromtimestamp(
                            f.last_modified_timestamp_ms / 1000.0),

                }

                if not args.no_raids and f.type == 0:
                    if f.HasField('raid_info'):
                        raids[f.id] = {
                            'gym_id': f.id,
                            'level': raid_info.raid_level,
                            'spawn': datetime.datetime.utcfromtimestamp(
                                raid_info.raid_spawn_ms / 1000.0),
                            'start': datetime.datetime.utcfromtimestamp(
                                raid_info.raid_battle_ms / 1000.0),
                            'end': datetime.datetime.utcfromtimestamp(
                                raid_info.raid_end_ms / 1000.0),
                            'pokemon_id': None,
                            'cp': None,
                            'move_1': None,
                            'move_2': None
                        }

                        if raid_info.HasField('raid_pokemon'):
                            raid_pokemon = raid_info.raid_pokemon
                            raids[f.id].update({
                                'pokemon_id': raid_pokemon.pokemon_id,
                                'cp': raid_pokemon.cp,
                                'move_1': raid_pokemon.move_1,
                                'move_2': raid_pokemon.move_2
                            })

                        if ('egg' in args.wh_types and
                                raids[f.id]['pokemon_id'] is None) or (
                                    'raid' in args.wh_types and
                                    raids[f.id]['pokemon_id'] is not None):
                            wh_raid = raids[f.id].copy()
                            wh_raid.update({
                                'gym_id': b64_gym_id,
                                'team_id': f.owned_by_team,
                                'spawn': raid_info.raid_spawn_ms / 1000,
                                'start': raid_info.raid_battle_ms / 1000,
                                'end': raid_info.raid_end_ms / 1000,
                                'latitude': f.latitude,
                                'longitude': f.longitude
                            })
                            wh_update_queue.put(('raid', wh_raid))

        # Let db do it's things while we try to spin.
        if args.pokestop_spinning:
            for f in forts:
                # Spin Pokestop with 50% chance.
                if f.type == 1 and pokestop_spinnable(f, scan_coords):
                    spin_pokestop(api, account, args, f, scan_coords)

        # Helping out the GC.
        del forts

    log.info('Parsing found Pokemon: %d (%d filtered), nearby: %d, ' +
             'pokestops: %d, gyms: %d, raids: %d.',
             len(pokemon) + skipped,
             filtered,
             nearby_pokemon,
             len(pokestops) + stopsskipped,
             len(gyms),
             len(raids))

    log.debug('Skipped Pokemon: %d, pokestops: %d.', skipped, stopsskipped)

    # Look for spawnpoints within scan_loc that are not here to see if we
    # can narrow down tth window.
    for sp in ScannedLocation.linked_spawn_points(scan_location['cellid']):
        if sp['missed_count'] > 5:
                continue

        if sp['id'] in sp_id_list:
            # Don't overwrite changes from this parse with DB version.
            sp = spawn_points[sp['id']]
        else:
            # If the cell has completed, we need to classify all
            # the SPs that were not picked up in the scan
            if just_completed:
                SpawnpointDetectionData.classify(sp, scan_location, now_secs)
                spawn_points[sp['id']] = sp
            if SpawnpointDetectionData.unseen(sp, now_secs):
                spawn_points[sp['id']] = sp
            endpoints = SpawnPoint.start_end(sp, args.spawn_delay)
            if clock_between(endpoints[0], now_secs, endpoints[1]):
                sp['missed_count'] += 1
                spawn_points[sp['id']] = sp
                log.warning('%s kind spawnpoint %s has no Pokemon %d times'
                            ' in a row.',
                            sp['kind'], sp['id'], sp['missed_count'])
                log.info('Possible causes: Still doing initial scan, super'
                         ' rare double spawnpoint during')
                log.info('hidden period, or Niantic has removed '
                         'spawnpoint.')

        if (not SpawnPoint.tth_found(sp) and scan_location['done'] and
                (now_secs - sp['latest_seen'] -
                 args.spawn_delay) % 3600 < 60):
            # Warning: python uses modulo as the least residue, not as
            # remainder, so we don't apply it to the result. Just a
            # safety measure until we can guarantee there's never a negative
            # result.
            log.warning('Spawnpoint %s was unable to locate a TTH, with '
                        'only %ss after Pokemon last seen.', sp['id'],
                        (now_secs % 3600 - sp['latest_seen'] % 3600))
            log.info('Restarting current 15 minute search for TTH.')
            if sp['id'] not in sp_id_list:
                SpawnpointDetectionData.classify(sp, scan_location, now_secs)
            sp['latest_seen'] = (sp['latest_seen'] - 60) % 3600
            sp['earliest_unseen'] = (
                sp['earliest_unseen'] + 14 * 60) % 3600
            spawn_points[sp['id']] = sp

    db_update_queue.put((ScannedLocation, {0: scan_location}))

    if pokemon:
        db_update_queue.put((Pokemon, pokemon))
    if pokestops:
        db_update_queue.put((Pokestop, pokestops))
    if gyms:
        db_update_queue.put((Gym, gyms))
    if raids:
        db_update_queue.put((Raid, raids))
    if spawn_points:
        db_update_queue.put((SpawnPoint, spawn_points))
        db_update_queue.put((ScanSpawnPoint, scan_spawn_points))
        if sightings:
            db_update_queue.put((SpawnpointDetectionData, sightings))

    if not nearby_pokemon and not wild_pokemon:
        # After parsing the forts, we'll mark this scan as bad due to
        # a possible speed violation.
        return {
            'count': wild_pokemon_count + forts_count,
            'gyms': gyms,
            'sp_id_list': sp_id_list,
            'bad_scan': True,
            'scan_secs': now_secs
        }

    return {
        'count': wild_pokemon_count + forts_count,
        'gyms': gyms,
        'sp_id_list': sp_id_list,
        'bad_scan': False,
        'scan_secs': now_secs
    }


def encounter_pokemon(args, pokemon, account, api, account_sets, status,
                      key_scheduler):
    using_accountset = False
    hlvl_account = None
    pokemon_id = None
    result = False
    try:
        hlvl_api = None
        pokemon_id = pokemon.pokemon_data.pokemon_id
        scan_location = [pokemon.latitude, pokemon.longitude]
        # If the host has L30s in the regular account pool, we
        # can just use the current account.
        if account['level'] >= 30:
            hlvl_account = account
            hlvl_api = api
        else:
            # Get account to use for IV and CP scanning.
            hlvl_account = account_sets.next('30', scan_location)
            using_accountset = True

        time.sleep(args.encounter_delay)

        # If we didn't get an account, we can't encounter.
        if not hlvl_account:
            log.error('No L30 accounts are available, please' +
                      ' consider adding more. Skipping encounter.')
            return False

        # Logging.
        log.info('Encountering Pokemon ID %s with account %s at %s, %s.',
                 pokemon_id, hlvl_account['username'], scan_location[0],
                 scan_location[1])

        # If not args.no_api_store is enabled, we need to
        # re-use an old API object if it's stored and we're
        # using an account from the AccountSet.
        if not args.no_api_store and using_accountset:
            hlvl_api = hlvl_account.get('api', None)

        # Make new API for this account if we're not using an
        # API that's already logged in.
        if not hlvl_api:
            hlvl_api = setup_api(args, status, hlvl_account)

        # If the already existent API is using a proxy but
        # it's not alive anymore, we need to get a new proxy.
        elif (args.proxy and
              (hlvl_api._session.proxies['http'] not in args.proxy)):
            proxy_idx, proxy_new = get_new_proxy(args)
            hlvl_api.set_proxy({
                'http': proxy_new,
                'https': proxy_new})
            hlvl_api._auth_provider.set_proxy({
                'http': proxy_new,
                'https': proxy_new})

        # Hashing key.
        # TODO: Rework inefficient threading.
        if args.hash_key:
            key = key_scheduler.next()
            log.debug('Using hashing key %s for this encounter.', key)
            hlvl_api.activate_hash_server(key)

        # We have an API object now. If necessary, store it.
        if using_accountset and not args.no_api_store:
            hlvl_account['api'] = hlvl_api

        # Set location.
        hlvl_api.set_position(*scan_location)

        # Log in.
        check_login(args, hlvl_account, hlvl_api, status['proxy_url'])
        encounter_level = hlvl_account['level']

        # User error -> we skip freeing the account.
        if encounter_level < 30:
            log.warning('Expected account of level 30 or higher, ' +
                        'but account %s is only level %d',
                        hlvl_account['username'], encounter_level)
            return False

        # Encounter Pokémon.
        encounter_result = encounter(
            hlvl_api, hlvl_account, pokemon.encounter_id,
            pokemon.spawn_point_id, scan_location)

        # Handle errors.
        if encounter_result:
            enc_responses = encounter_result['responses']
            # Check for captcha.
            if 'CHECK_CHALLENGE' in enc_responses:
                captcha_url = enc_responses['CHECK_CHALLENGE'].challenge_url

                # Throw warning but finish parsing.
                if len(captcha_url) > 1:
                    # Flag account.
                    hlvl_account['captcha'] = True
                    log.error('Account %s encountered a captcha.' +
                              ' Account will not be used.',
                              hlvl_account['username'])

            if ('ENCOUNTER' in enc_responses and
                    enc_responses['ENCOUNTER'].status != 1):
                log.error('There was an error encountering Pokemon ID %s with '
                          + 'account %s: %d.', pokemon_id,
                          hlvl_account['username'],
                          enc_responses['ENCOUNTER'].status)
            else:
                pokemon_info = enc_responses[
                    'ENCOUNTER'].wild_pokemon.pokemon_data
                # Logging: let the user know we succeeded.
                log.info('Encounter for Pokemon ID %s at %s, %s ' +
                         'successful: %s/%s/%s, %s CP.', pokemon_id,
                         pokemon.latitude, pokemon.longitude,
                         pokemon_info.individual_attack,
                         pokemon_info.individual_defense,
                         pokemon_info.individual_stamina, pokemon_info.cp)

                result = pokemon_info

    except Exception as e:
        # Account may not be selected yet.
        if hlvl_account:
            log.warning('Exception occured during encounter with'
                        ' high-level account %s.',
                        hlvl_account['username'])
        log.exception('There was an error encountering Pokemon ID %s: %s.',
                      pokemon_id,
                      e)

    # We're done with the encounter. If it's from an
    # AccountSet, release account back to the pool.
    if using_accountset:
        account_sets.release(hlvl_account)

    return result


def parse_gyms(args, gym_responses, wh_update_queue, db_update_queue):
    gym_details = {}
    gym_members = {}
    gym_pokemon = {}
    i = 0
    for g in gym_responses.values():
        gym_state = g.gym_status_and_defenders
        gym_id = gym_state.pokemon_fort_proto.id

        gym_details[gym_id] = {
            'gym_id': gym_id,
            'name': g.name,
            'description': g.description,
            'url': g.url
        }

        if 'gym-info' in args.wh_types:
            webhook_data = {
                'id': str(gym_id),
                'latitude': gym_state.pokemon_fort_proto.latitude,
                'longitude': gym_state.pokemon_fort_proto.longitude,
                'team': gym_state.pokemon_fort_proto.owned_by_team,
                'name': g.name,
                'description': g.description,
                'url': g.url,
                'pokemon': [],
            }

        for member in gym_state.gym_defender:
            pokemon = member.motivated_pokemon.pokemon
            gym_members[i] = {
                'gym_id':
                    gym_id,
                'pokemon_uid':
                    pokemon.id,
                'cp_decayed':
                    member.motivated_pokemon.cp_now,
                'deployment_time':
                    datetime.datetime.utcnow() -
                    datetime.timedelta(milliseconds=member.deployment_totals
                              .deployment_duration_ms)
            }
            gym_pokemon[i] = {
                'pokemon_uid': pokemon.id,
                'pokemon_id': pokemon.pokemon_id,
                'cp': member.motivated_pokemon.cp_when_deployed,
                'num_upgrades': pokemon.num_upgrades,
                'move_1': pokemon.move_1,
                'move_2': pokemon.move_2,
                'height': pokemon.height_m,
                'weight': pokemon.weight_kg,
                'stamina': pokemon.stamina,
                'stamina_max': pokemon.stamina_max,
                'cp_multiplier': pokemon.cp_multiplier,
                'additional_cp_multiplier': pokemon.additional_cp_multiplier,
                'iv_defense': pokemon.individual_defense,
                'iv_stamina': pokemon.individual_stamina,
                'iv_attack': pokemon.individual_attack,
                'costume': pokemon.pokemon_display.costume,
                'form': pokemon.pokemon_display.form,
                'shiny': pokemon.pokemon_display.shiny,
                'last_seen': datetime.datetime.utcnow(),
            }

            if 'gym-info' in args.wh_types:
                wh_pokemon = gym_pokemon[i].copy()
                del wh_pokemon['last_seen']
                wh_pokemon.update({
                    'cp_decayed':
                        member.motivated_pokemon.cp_now,
                    'deployment_time': calendar.timegm(
                        gym_members[i]['deployment_time'].timetuple())
                })
                webhook_data['pokemon'].append(wh_pokemon)

            i += 1
        if 'gym-info' in args.wh_types:
            wh_update_queue.put(('gym_details', webhook_data))

    # All this database stuff is synchronous (not using the upsert queue) on
    # purpose.  Since the search workers load the GymDetails model from the
    # database to determine if a gym needs to be rescanned, we need to be sure
    # the GymDetails get fully committed to the database before moving on.
    #
    # We _could_ synchronously upsert GymDetails, then queue the other tables
    # for upsert, but that would put that Gym's overall information in a weird
    # non-atomic state.

    # Upsert all the models.
    if gym_details:
        db_update_queue.put((GymDetails, gym_details))
    if gym_pokemon:
        db_update_queue.put((GymPokemon, gym_pokemon))

    # Get rid of all the gym members, we're going to insert new records.
    if gym_details:
        with GymMember.database().execution_context():
            DeleteQuery(GymMember).where(
                GymMember.gym_id << gym_details.keys()).execute()

    # Insert new gym members.
    if gym_members:
        db_update_queue.put((GymMember, gym_members))

    log.info('Upserted gyms: %d, gym members: %d.',
             len(gym_details),
             len(gym_members))


def db_updater(q, db):
    # The forever loop.
    while True:
        try:
            # Loop the queue.
            while True:
                model, data = q.get()

                start_timer = default_timer()
                bulk_upsert(model, data, db)
                q.task_done()

                log.debug('Upserted to %s, %d records (upsert queue '
                          'remaining: %d) in %.6f seconds.',
                          model.__name__,
                          len(data),
                          q.qsize(),
                          default_timer() - start_timer)

                # Helping out the GC.
                del model
                del data

                if q.qsize() > 50:
                    log.warning(
                        "DB queue is > 50 (@%d); try increasing --db-threads.",
                        q.qsize())

        except Exception as e:
            log.exception('Exception in db_updater: %s', repr(e))
            time.sleep(5)


def clean_db_loop(args):
    # Run regular database cleanup once every minute.
    regular_cleanup_secs = 60
    # Run full database cleanup once every 10 minutes.
    full_cleanup_timer = default_timer()
    full_cleanup_secs = 600
    while True:
        try:
            db_cleanup_regular()

            # Remove old worker status entries.
            if args.db_cleanup_worker > 0:
                db_cleanup_worker_status(args.db_cleanup_worker)

            # Check if it's time to run full database cleanup.
            now = default_timer()
            if now - full_cleanup_timer > full_cleanup_secs:
                # Remove old pokemon spawns.
                if args.db_cleanup_pokemon > 0:
                    db_clean_pokemons(args.db_cleanup_pokemon)

                # Remove old gym data.
                if args.db_cleanup_gym > 0:
                    db_clean_gyms(args.db_cleanup_gym)

                # Remove old and extinct spawnpoint data.
                if args.db_cleanup_spawnpoint > 0:
                    db_clean_spawnpoints(args.db_cleanup_spawnpoint)

                # Remove old pokestop and gym locations.
                if args.db_cleanup_forts > 0:
                    db_clean_forts(args.db_cleanup_forts)

                log.info('Full database cleanup completed.')
                full_cleanup_timer = now

            time.sleep(regular_cleanup_secs)
        except Exception as e:
            log.exception('Database cleanup failed: %s.', e)


def db_cleanup_regular():
    log.debug('Regular database cleanup started.')
    start_timer = default_timer()

    now = datetime.datetime.utcnow()
    # http://docs.peewee-orm.com/en/latest/peewee/database.html#advanced-connection-management
    # When using an execution context, a separate connection from the pool
    # will be used inside the wrapped block and a transaction will be started.
    with Token.database().execution_context():
        # Remove unusable captcha tokens.
        query = (Token
                 .delete()
                 .where(Token.last_updated < now - datetime.timedelta(seconds=120)))
        query.execute()

        # Remove active modifier from expired lured pokestops.
        query = (Pokestop
                 .update(lure_expiration=None, active_fort_modifier=None)
                 .where(Pokestop.lure_expiration < now))
        query.execute()

        # Remove expired or inactive hashing keys.
        query = (HashKeys
                 .delete()
                 .where((HashKeys.expires < now - datetime.timedelta(days=1)) |
                        (HashKeys.last_updated < now - datetime.timedelta(days=7))))
        query.execute()

    time_diff = default_timer() - start_timer
    log.debug('Completed regular cleanup in %.6f seconds.', time_diff)


def db_cleanup_worker_status(age_minutes):
    log.debug('Beginning cleanup of old worker status.')
    start_timer = default_timer()

    worker_status_timeout = datetime.datetime.utcnow() - datetime.timedelta(minutes=age_minutes)

    with MainWorker.database().execution_context():
        # Remove status information from inactive instances.
        query = (MainWorker
                 .delete()
                 .where(MainWorker.last_modified < worker_status_timeout))
        query.execute()

        # Remove worker status information that are inactive.
        query = (WorkerStatus
                 .delete()
                 .where(MainWorker.last_modified < worker_status_timeout))
        query.execute()

    time_diff = default_timer() - start_timer
    log.debug('Completed cleanup of old worker status in %.6f seconds.',
              time_diff)


def db_clean_pokemons(age_hours):
    log.debug('Beginning cleanup of old pokemon spawns.')
    start_timer = default_timer()

    pokemon_timeout = datetime.datetime.utcnow() - datetime.timedelta(hours=age_hours)

    with PokestopMember.database().execution_context():
        query = (PokestopMember
             .delete()
             .where(PokestopMember.disappear_time < pokemon_timeout))
        rows = query.execute()
        log.debug('Deleted %d old PokestopMember entries.', rows)


    with Pokemon.database().execution_context():
        query = (Pokemon
                 .delete()
                 .where(Pokemon.disappear_time < pokemon_timeout))
        rows = query.execute()
        log.debug('Deleted %d old Pokemon entries.', rows)

    time_diff = default_timer() - start_timer
    log.debug('Completed cleanup of old pokemon spawns in %.6f seconds.',
              time_diff)


def db_clean_gyms(age_hours, gyms_age_days=30):
    log.debug('Beginning cleanup of old gym data.')
    start_timer = default_timer()

    gym_info_timeout = datetime.datetime.utcnow() - datetime.timedelta(hours=age_hours)

    with Gym.database().execution_context():
        # Remove old GymDetails entries.
        query = (GymDetails
                 .delete()
                 .where(GymDetails.last_scanned < gym_info_timeout))
        rows = query.execute()
        log.debug('Deleted %d old GymDetails entries.', rows)

        # Remove old Raid entries.
        query = (Raid
                 .delete()
                 .where(Raid.end < gym_info_timeout))
        rows = query.execute()
        log.debug('Deleted %d old Raid entries.', rows)

        # Remove old GymMember entries.
        query = (GymMember
                 .delete()
                 .where(GymMember.last_scanned < gym_info_timeout))
        rows = query.execute()
        log.debug('Deleted %d old GymMember entries.', rows)

        # Remove old GymPokemon entries.
        query = (GymPokemon
                 .delete()
                 .where(GymPokemon.last_seen < gym_info_timeout))
        rows = query.execute()
        log.debug('Deleted %d old GymPokemon entries.', rows)

    time_diff = default_timer() - start_timer
    log.debug('Completed cleanup of old gym data in %.6f seconds.',
              time_diff)


def db_clean_spawnpoints(age_hours, missed=5):
    log.debug('Beginning cleanup of old spawnpoint data.')
    start_timer = default_timer()
    # Maximum number of variables to include in a single query.
    step = 500

    spawnpoint_timeout = datetime.datetime.utcnow() - datetime.timedelta(hours=age_hours)

    with SpawnPoint.database().execution_context():
        # Select old SpawnPoint entries.
        query = (SpawnPoint
                 .select(SpawnPoint.id)
                 .where((SpawnPoint.last_scanned < spawnpoint_timeout) &
                        (SpawnPoint.missed_count > missed))
                 .dicts())
        old_sp = [(sp['id']) for sp in query]

        num_records = len(old_sp)
        log.debug('Found %d old SpawnPoint entries.', num_records)

        # Remove SpawnpointDetectionData entries associated to old spawnpoints.
        num_rows = 0
        for i in range(0, num_records, step):
            query = (SpawnpointDetectionData
                     .delete()
                     .where((SpawnpointDetectionData.spawnpoint_id <<
                             old_sp[i:min(i + step, num_records)])))
            num_rows += query.execute()

        # Remove old SpawnPointDetectionData entries.
        query = (SpawnpointDetectionData
                 .delete()
                 .where((SpawnpointDetectionData.scan_time <
                         spawnpoint_timeout)))
        num_rows += query.execute()
        log.debug('Deleted %d old SpawnpointDetectionData entries.', num_rows)

        # Select ScannedLocation entries associated to old spawnpoints.
        sl_delete = set()
        for i in range(0, num_records, step):
            query = (ScanSpawnPoint
                     .select()
                     .where((ScanSpawnPoint.spawnpoint <<
                             old_sp[i:min(i + step, num_records)]))
                     .dicts())
            for sp in query:
                sl_delete.add(sp['scannedlocation'])
        log.debug('Found %d ScannedLocation entries from old spawnpoints.',
                  len(sl_delete))

        # Remove ScanSpawnPoint entries associated to old spawnpoints.
        num_rows = 0
        for i in range(0, num_records, step):
            query = (ScanSpawnPoint
                     .delete()
                     .where((ScanSpawnPoint.spawnpoint <<
                             old_sp[i:min(i + step, num_records)])))
            num_rows += query.execute()
        log.debug('Deleted %d ScanSpawnPoint entries from old spawnpoints.',
                  num_rows)

        # Remove old and invalid SpawnPoint entries.
        num_rows = 0
        for i in range(0, num_records, step):
            query = (SpawnPoint
                     .delete()
                     .where((SpawnPoint.id <<
                             old_sp[i:min(i + step, num_records)])))
            num_rows += query.execute()
        log.debug('Deleted %d old SpawnPoint entries.', num_rows)

        sl_delete = list(sl_delete)
        num_records = len(sl_delete)

        # Remove ScanSpawnPoint entries associated with old scanned locations.
        num_rows = 0
        for i in range(0, num_records, step):
            query = (ScanSpawnPoint
                     .delete()
                     .where((ScanSpawnPoint.scannedlocation <<
                             sl_delete[i:min(i + step, num_records)])))
            num_rows += query.execute()
        log.debug('Deleted %d ScanSpawnPoint entries from old scan locations.',
                  num_rows)

        # Remove ScannedLocation entries associated with old spawnpoints.
        num_rows = 0
        for i in range(0, num_records, step):
            query = (ScannedLocation
                     .delete()
                     .where((ScannedLocation.cellid <<
                             sl_delete[i:min(i + step, num_records)]) &
                            (ScannedLocation.last_modified <
                             spawnpoint_timeout)))
            num_rows += query.execute()
        log.debug('Deleted %d ScannedLocation entries from old spawnpoints.',
                  num_rows)

    time_diff = default_timer() - start_timer
    log.debug('Completed cleanup of old spawnpoint data in %.6f seconds.',
              time_diff)


def db_clean_forts(age_hours):
    log.debug('Beginning cleanup of old forts.')
    start_timer = default_timer()

    fort_timeout = datetime.datetime.utcnow() - datetime.timedelta(hours=age_hours)

    with Gym.database().execution_context():
        # Remove old Gym entries.
        query = (Gym
                 .delete()
                 .where(Gym.last_scanned < fort_timeout))
        rows = query.execute()
        log.debug('Deleted %d old Gym entries.', rows)

        # Remove old Pokestop entries.
        query = (Pokestop
                 .delete()
                 .where(Pokestop.last_updated < fort_timeout))
        rows = query.execute()
        log.debug('Deleted %d old Pokestop entries.', rows)

    time_diff = default_timer() - start_timer
    log.debug('Completed cleanup of old forts in %.6f seconds.',
              time_diff)


def bulk_upsert(cls, data, db):
    rows = data.values()
    num_rows = len(rows)
    i = 0

    # This shouldn't happen, ever, but anyways...
    if num_rows < 1:
        return

    # We used to support SQLite and it has a default max 999 parameters,
    # so we limited how many rows we insert for it.
    # Oracle: 64000
    # MySQL: 65535
    # PostgreSQL: 34464
    # Sqlite: 999
    step = 500

    if db.is_closed():
        log.debug("Database connection is closed, connect again")
        db = MyRetryDB(
            args.db_name,
            user=args.db_user,
            password=args.db_pass,
            host=args.db_host,
            port=args.db_port,
            stale_timeout=30,
            max_connections=None,
            charset='utf8mb4')

    # Prepare for our query.
    conn = db.get_conn()
    cursor = db.get_cursor()

    # We build our own INSERT INTO ... ON DUPLICATE KEY UPDATE x=VALUES(x)
    # query, making sure all data is properly escaped. We use
    # placeholders for VALUES(%s, %s, ...) so we can use executemany().
    # We use peewee's InsertQuery to retrieve the fields because it
    # takes care of peewee's internals (e.g. required default fields).
    query = InsertQuery(cls, rows=[list(rows)[0]])
    # Take the first row. We need to call _iter_rows() for peewee internals.
    # Using next() for a single item is not considered "pythonic".
    first_row = {}
    for row in query._iter_rows():
        first_row = row
        break
    # Convert the row to its fields, sorted by peewee.
    row_fields = sorted(first_row.keys(), key=lambda x: x._sort_key)
    row_fields = list(map(lambda x: x.name, row_fields))
    # Translate to proper column name, e.g. foreign keys.
    db_columns = list([peewee_attr_to_col(cls, f) for f in row_fields])

    # Store defaults so we can fall back to them if a value
    # isn't set.
    defaults = {}

    for f in cls._meta.fields.values():
        # Use DB column name as key.
        field_name = f.name
        field_default = cls._meta.defaults.get(f, None)
        defaults[field_name] = field_default

    # Assign fields, placeholders and assignments after defaults
    # so our lists/keys stay in order.
    table = '`'+(conn.escape_string(cls._meta.db_table))+'`'
    escaped_fields = ['`'+(conn.escape_string(f))+'`' for f in db_columns]
    placeholders = ['%s' for escaped_field in escaped_fields]
    assignments = ['{x} = VALUES({x})'.format(
        x=escaped_field
    ) for escaped_field in escaped_fields]

    # We build our own MySQL query because peewee only supports
    # REPLACE INTO for upserting, which deletes the old row before
    # adding the new one, giving a serious performance hit.
    query_string = ('INSERT INTO {table} ({fields}) VALUES'
                    + ' ({placeholders}) ON DUPLICATE KEY UPDATE'
                    + ' {assignments}')

    # Prepare transaction.
    with db.atomic():
        while i < num_rows:
            start = i
            end = min(i + step, num_rows)
            name = cls.__name__

            log.debug('Inserting items %d to %d for %s.', start, end, name)

            try:
                # Turn off FOREIGN_KEY_CHECKS on MySQL, because apparently it's
                # unable to recognize strings to update unicode keys for
                # foreign key fields, thus giving lots of foreign key
                # constraint errors.
                db.execute_sql('SET FOREIGN_KEY_CHECKS=0;')

                # Time to bulk upsert our data. Convert objects to a list of
                # values for executemany(), and fall back to defaults if
                # necessary.
                batch = []
                batch_rows = list(rows)[i:min(i + step, num_rows)]

                # We pop them off one by one so we can gradually release
                # memory as we pass each item. No duplicate memory usage.
                while len(batch_rows) > 0:
                    row = batch_rows.pop()
                    row_data = []

                    # Parse rows, build arrays of values sorted via row_fields.
                    for field in row_fields:
                        # Take a default if we need it.
                        if field not in row:
                            default = defaults.get(field, None)

                            # peewee's defaults can be callable, e.g. current
                            # time. We only call when needed to insert.
                            if callable(default):
                                default = default()

                            row[field] = default

                        # Append to keep the exact order, and only these
                        # fields.
                        row_data.append(row[field])
                    # Done preparing, add it to the batch.
                    batch.append(row_data)

                # Format query and go.
                formatted_query = query_string.format(
                    table=table,
                    fields=', '.join(escaped_fields),
                    placeholders=', '.join(placeholders),
                    assignments=', '.join(assignments)
                )

                cursor.executemany(formatted_query, batch)

                db.execute_sql('SET FOREIGN_KEY_CHECKS=1;')

            except Exception as e:
                # If there is a DB table constraint error, dump the data and
                # don't retry.
                #
                # Unrecoverable error strings:
                unrecoverable = ['constraint', 'has no attribute',
                                 'peewee.IntegerField object at']
                has_unrecoverable = filter(
                    lambda x: x in str(e), unrecoverable)
                if has_unrecoverable:
                    log.exception('%s. Data is:', repr(e))
                    log.warning(data.items())
                else:
                    log.warning('%s... Retrying...', repr(e))
                    time.sleep(1)
                    continue

            i += step


def create_tables(db):
    tables = [Pokemon, Pokestop, Gym, Raid, ScannedLocation, GymDetails,
              GymMember, GymPokemon, MainWorker, WorkerStatus,
              SpawnPoint, ScanSpawnPoint, SpawnpointDetectionData,
              Token, LocationAltitude, PlayerLocale, HashKeys, Weather, DeviceWorker, PokestopMember,
              Quest, PokestopDetails]
    with db.execution_context():
        for table in tables:
            if not table.table_exists():
                log.info('Creating table: %s', table.__name__)
                db.create_tables([table], safe=True)
            else:
                log.debug('Skipping table %s, it already exists.',
                          table.__name__)


def drop_tables(db):
    tables = [Pokemon, Pokestop, Gym, Raid, ScannedLocation, Versions,
              GymDetails, GymMember, GymPokemon, MainWorker,
              WorkerStatus, SpawnPoint, ScanSpawnPoint,
              SpawnpointDetectionData, LocationAltitude, PlayerLocale,
              Token, HashKeys, Weather, DeviceWorker, PokestopMember,
              Quest, PokestopDetails]
    with db.execution_context():
        db.execute_sql('SET FOREIGN_KEY_CHECKS=0;')
        for table in tables:
            if table.table_exists():
                log.info('Dropping table: %s', table.__name__)
                db.drop_tables([table], safe=True)

        db.execute_sql('SET FOREIGN_KEY_CHECKS=1;')


def verify_table_encoding(db):
    with db.execution_context():

        cmd_sql = '''
            SELECT table_name FROM information_schema.tables WHERE
            table_collation != "utf8mb4_unicode_ci"
            AND table_schema = "%s";
            ''' % args.db_name
        change_tables = db.execute_sql(cmd_sql)

        cmd_sql = "SHOW tables;"
        tables = db.execute_sql(cmd_sql)

        if change_tables.rowcount > 0:
            log.info('Changing collation and charset on %s tables.',
                     change_tables.rowcount)

            if change_tables.rowcount == tables.rowcount:
                log.info('Changing whole database,' +
                         ' this might a take while.')

            db.execute_sql('SET FOREIGN_KEY_CHECKS=0;')
            for table in change_tables:
                log.debug('Changing collation and charset on table %s.',
                          table[0])
                cmd_sql = '''ALTER TABLE %s CONVERT TO CHARACTER SET utf8mb4
                            COLLATE utf8mb4_unicode_ci;''' % str(table[0])
                db.execute_sql(cmd_sql)
            db.execute_sql('SET FOREIGN_KEY_CHECKS=1;')


def verify_database_schema(db):
    if not Versions.table_exists():
        db.create_tables([Versions])

        if ScannedLocation.table_exists():
            # Versions table doesn't exist, but there are tables. This must
            # mean the user is coming from a database that existed before we
            # started tracking the schema version. Perform a full upgrade.
            InsertQuery(Versions, {Versions.key: 'schema_version',
                                   Versions.val: 0}).execute()
            database_migrate(db, 0)
        else:
            InsertQuery(Versions, {Versions.key: 'schema_version',
                                   Versions.val: db_schema_version}).execute()

    else:
        db_ver = Versions.get(Versions.key == 'schema_version').val

        if db_ver < db_schema_version:
            if not database_migrate(db, db_ver):
                log.error('Error migrating database')
                sys.exit(1)

        elif db_ver > db_schema_version:
            log.error('Your database version (%i) appears to be newer than '
                      'the code supports (%i).', db_ver, db_schema_version)
            log.error('Please upgrade your code base or drop all tables in '
                      'your database.')
            sys.exit(1)
    db.close()


def database_migrate(db, old_ver):
    # Update database schema version.
    Versions.update(val=db_schema_version).where(
        Versions.key == 'schema_version').execute()

    log.info('Detected database version %i, updating to %i...',
             old_ver, db_schema_version)

    # Perform migrations here.
    migrator = MySQLMigrator(db)

    if old_ver < 20:
        migrate(
            migrator.drop_column('gym', 'gym_points'),
            migrator.add_column('gym', 'slots_available',
                                SmallIntegerField(null=False, default=0)),
            migrator.add_column('gymmember', 'cp_decayed',
                                SmallIntegerField(null=False, default=0)),
            migrator.add_column('gymmember', 'deployment_time',
                                DateTimeField(
                                    null=False, default=datetime.datetime.utcnow())),
            migrator.add_column('gym', 'total_cp',
                                SmallIntegerField(null=False, default=0))
        )

    if old_ver < 21:
        # First rename all tables being modified.
        db.execute_sql('RENAME TABLE `pokemon` TO `pokemon_old`;')
        db.execute_sql(
            'RENAME TABLE `locationaltitude` TO `locationaltitude_old`;')
        db.execute_sql(
            'RENAME TABLE `scannedlocation` TO `scannedlocation_old`;')
        db.execute_sql('RENAME TABLE `spawnpoint` TO `spawnpoint_old`;')
        db.execute_sql('RENAME TABLE `spawnpointdetectiondata` TO ' +
                       '`spawnpointdetectiondata_old`;')
        db.execute_sql('RENAME TABLE `gymmember` TO `gymmember_old`;')
        db.execute_sql('RENAME TABLE `gympokemon` TO `gympokemon_old`;')
        db.execute_sql(
            'RENAME TABLE `scanspawnpoint`  TO `scanspawnpoint_old`;')
        # Then create all tables that we renamed with the proper fields.
        create_tables(db)
        # Insert data back with the correct format
        db.execute_sql(
            'INSERT INTO `pokemon` SELECT ' +
            'FROM_BASE64(encounter_id) as encounter_id, ' +
            'CONV(spawnpoint_id, 16,10) as spawnpoint_id, ' +
            'pokemon_id, latitude, longitude, disappear_time, ' +
            'individual_attack, individual_defense, individual_stamina, ' +
            'move_1, move_2, cp, cp_multiplier, weight, height, gender, ' +
            'form, last_modified ' +
            'FROM `pokemon_old`;')
        db.execute_sql(
            'INSERT INTO `locationaltitude` SELECT ' +
            'CONV(cellid, 16,10) as cellid, ' +
            'latitude, longitude, last_modified, altitude ' +
            'FROM `locationaltitude_old`;')
        db.execute_sql(
            'INSERT INTO `scannedlocation` SELECT ' +
            'CONV(cellid, 16,10) as cellid, ' +
            'latitude, longitude, last_modified, done, band1, band2, band3, ' +
            'band4, band5, midpoint, width ' +
            'FROM `scannedlocation_old`;')
        db.execute_sql(
            'INSERT INTO `spawnpoint` SELECT ' +
            'CONV(id, 16,10) as id, ' +
            'latitude, longitude, last_scanned, kind, links, missed_count, ' +
            'latest_seen, earliest_unseen ' +
            'FROM `spawnpoint_old`;')
        db.execute_sql(
            'INSERT INTO `spawnpointdetectiondata` ' +
            '(encounter_id, spawnpoint_id, scan_time, tth_secs) SELECT ' +
            'FROM_BASE64(encounter_id) as encounter_id, ' +
            'CONV(spawnpoint_id, 16,10) as spawnpoint_id, ' +
            'scan_time, tth_secs ' +
            'FROM `spawnpointdetectiondata_old`;')
        # A simple alter table does not work ¯\_(ツ)_/¯
        db.execute_sql(
            'INSERT INTO `gymmember` SELECT * FROM `gymmember_old`;')
        db.execute_sql(
            'INSERT INTO `gympokemon` SELECT * FROM `gympokemon_old`;')
        db.execute_sql(
            'INSERT INTO `scanspawnpoint` SELECT ' +
            'CONV(scannedlocation_id, 16,10) as scannedlocation_id, ' +
            'CONV(spawnpoint_id, 16,10) as spawnpoint_id ' +
            'FROM `scanspawnpoint_old`;')
        db.execute_sql(
            'ALTER TABLE `pokestop` MODIFY active_fort_modifier SMALLINT(6);')
        # Drop all _old tables
        db.execute_sql('DROP TABLE `scanspawnpoint_old`;')
        db.execute_sql('DROP TABLE `pokemon_old`;')
        db.execute_sql('DROP TABLE `locationaltitude_old`;')
        db.execute_sql('DROP TABLE `spawnpointdetectiondata_old`;')
        db.execute_sql('DROP TABLE `scannedlocation_old`;')
        db.execute_sql('DROP TABLE `spawnpoint_old`;')
        db.execute_sql('DROP TABLE `gymmember_old`;')
        db.execute_sql('DROP TABLE `gympokemon_old`;')

    if old_ver < 22:
        # Drop and add CONSTRAINT_2 with the <= fix.
        db.execute_sql('ALTER TABLE `spawnpoint` '
                       'DROP CONSTRAINT CONSTRAINT_2;')
        db.execute_sql('ALTER TABLE `spawnpoint` '
                       'ADD CONSTRAINT CONSTRAINT_2 ' +
                       'CHECK (`earliest_unseen` <= 3600);')

        # Drop and add CONSTRAINT_4 with the <= fix.
        db.execute_sql('ALTER TABLE `spawnpoint` '
                       'DROP CONSTRAINT CONSTRAINT_4;')
        db.execute_sql('ALTER TABLE `spawnpoint` '
                       'ADD CONSTRAINT CONSTRAINT_4 CHECK ' +
                       '(`latest_seen` <= 3600);')

    if old_ver < 23:
        db.drop_tables([WorkerStatus])
        db.drop_tables([MainWorker])

    if old_ver < 24:
        migrate(
            migrator.drop_index('pokemon', 'pokemon_disappear_time'),
            migrator.add_index('pokemon',
                               ('disappear_time', 'pokemon_id'), False)
        )

    if old_ver < 25:
        migrate(
            # Add `costume` column to `pokemon`
            migrator.add_column('pokemon', 'costume',
                                SmallIntegerField(null=True)),
            # Add `form` column to `gympokemon`
            migrator.add_column('gympokemon', 'form',
                                SmallIntegerField(null=True)),
            # Add `costume` column to `gympokemon`
            migrator.add_column('gympokemon', 'costume',
                                SmallIntegerField(null=True))
        )

    if old_ver < 26:
        migrate(
            # Add `park` column to `gym`
            migrator.add_column('gym', 'park', BooleanField(default=False))
        )

    if old_ver < 27:
        migrate(
            # Add `shiny` column to `gympokemon`
            migrator.add_column('gympokemon', 'shiny',
                                SmallIntegerField(null=True))
        )

    if old_ver < 28:
        migrate(
            migrator.add_column('pokemon', 'weather_boosted_condition',
                                SmallIntegerField(null=True))
        )

    if old_ver < 29:
        db.execute_sql('DROP TABLE `trainer`;')
        migrate(
            # drop trainer from gympokemon
            migrator.drop_column('gympokemon', 'trainer_name')
        )

    if old_ver < 30:
        db.execute_sql(
            'ALTER TABLE `hashkeys` '
            'MODIFY COLUMN `maximum` INTEGER,'
            'MODIFY COLUMN `remaining` INTEGER,'
            'MODIFY COLUMN `peak` INTEGER;'
        )

    if old_ver < 33:
        db.execute_sql(
            'ALTER TABLE `pokemon` MODIFY spawnpoint_id VARCHAR(100);'
        )

        db.execute_sql(
            'ALTER TABLE `spawnpoint` MODIFY id VARCHAR(100);'
        )

    if old_ver < 36:
        db.execute_sql(
            'ALTER TABLE `spawnpointdetectiondata` MODIFY spawnpoint_id VARCHAR(100);'
        )

    if old_ver < 38:
        migrate(
            migrator.add_column('deviceworker', 'last_updated',
                                DateTimeField(index=True, default=datetime.datetime.utcnow))
        )

    if old_ver < 40:
        db.execute_sql(
            'ALTER TABLE `deviceworker` MODIFY deviceid VARCHAR(100) NOT NULL;'
        )
        db.execute_sql(
            'ALTER TABLE `deviceworker` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;'
        )

    if old_ver < 41:
        migrate(
            migrator.add_column('gym', 'is_in_battle', BooleanField(default=False))
        )

    if old_ver < 42:
        migrate(
            migrator.add_column('gym', 'is_ex_raid_eligible', BooleanField(default=False))
        )

    if old_ver < 45:
        create_tables(db)

    if old_ver < 46 and old_ver > 40:
        db.execute_sql('DROP TABLE `deviceworker`;')
        create_tables(db)

    if old_ver < 47:
        create_tables(db)

    if old_ver < 48:
        migrate(
            migrator.add_column('deviceworker', 'name',
                                Utf8mb4CharField(max_length=100, default=""))
        )

    if old_ver < 49:
        migrate(
            migrator.add_column('deviceworker', 'discord_id',
                                Utf8mb4CharField(max_length=100, default=""))
        )

    if old_ver < 50:
        migrate(
            migrator.add_column('raid', 'form', SmallIntegerField(null=True))
        )

    if old_ver < 51:
        db.execute_sql(
            'ALTER TABLE `quest` ADD COLUMN `quest_json` LONGTEXT NULL AFTER `reward_amount`;'
        )

    if old_ver < 52:
        migrate(
            migrator.add_column('pokestop', 'active_pokemon_id', SmallIntegerField(null=True)),
            migrator.add_column('pokestop', 'active_pokemon_expiration', DateTimeField(index=True, null=True)),
        )

    if old_ver < 53:
        migrate(
            migrator.drop_index('pokestop', 'pokestop_active_fort_modifier'),
        )
        db.execute_sql(
            'ALTER TABLE `pokestop` MODIFY active_fort_modifier VARCHAR(100) NOT NULL;'
        )

    if old_ver < 54:
        db.execute_sql('DROP TABLE `deviceworker`;')
        create_tables(db)

    if old_ver < 55:
        create_tables(db)

    if old_ver < 56:
        migrate(
            migrator.add_column('quest', 'expiration', DateTimeField(index=True, null=True)),
        )

    if old_ver < 57:
        migrate(
            migrator.add_column('deviceworker', 'endpoint', Utf8mb4CharField(max_length=2000, default="")),
        )

    if old_ver < 58:
        migrate(
            migrator.add_column('scannedlocation', 'fortradius', UBigIntegerField(default=450)),
            migrator.add_column('scannedlocation', 'monradius', UBigIntegerField(default=70))
        )
    if old_ver < 59:
        migrate(
            migrator.add_column('scannedlocation', 'scanningforts', SmallIntegerField(default=0))
        )
    if old_ver < 60:
        migrate(
            migrator.drop_column('deviceworker', 'discord_id'),
            migrator.add_column('deviceworker', 'username', Utf8mb4CharField(max_length=100, default="")),
        )
    if old_ver < 61:
        db.execute_sql('DROP TABLE `geofence`;')
    if old_ver < 62:
        migrate(
            migrator.add_column('deviceworker', 'requestedEndpoint', Utf8mb4CharField(max_length=100, default="")),
            migrator.add_column('deviceworker', 'pogoversion', Utf8mb4CharField(max_length=10, default=""))
        )

    # Always log that we're done.
    log.info('Schema upgrade complete.')
    return True
