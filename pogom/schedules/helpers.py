from datetime import datetime, timedelta


def get_geofences_with_active_raids(geofences, args):
    result = ""
    now = datetime.utcnow()
    for area in geofences.geofenced_areas:
        localtime = now + timedelta(minutes=area["timezone_offset"])
        if localtime.hour >= args.first_raid_hour and localtime.hour <= args.last_raid_hour:
            if result != "":
                result += ","
            result += area["name"]
    return result


def get_active_devices(devices, username=""):
    result = []

    for uuid, dev in devices.iteritems():
        last_updated = dev['last_updated']
        difference = (datetime.utcnow() - last_updated).total_seconds()
        if difference > 300 and dev['fetching'] != 'IDLE':
            dev['fetching'] = 'IDLE'
        last_scanned = dev['last_scanned']
        if last_scanned is None and dev['scanning'] != -1:
            dev['scanning'] = -1
        else:
            difference = (datetime.utcnow() - last_scanned).total_seconds()
            if difference > 60 and dev['scanning'] == 1:
                dev['scanning'] = 0

        if (dev['scanning'] == 1 or dev['fetching'] != 'IDLE') and (username == "" or username == dev["username"]):
            result.append(dev)

    return result


def get_scheduled_devices(devices, schedulename="", username=""):
    result = []
    active_devices = get_active_devices(devices, username)

    for dev in active_devices:
        if dev['scheduled'] and (schedulename == "" or schedulename in dev["endpoint"] or schedulename in dev["requestedEndpoint"]):
            result.append(dev)

    return result
