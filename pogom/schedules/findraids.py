from helpers import get_scheduled_devices, get_geofences_with_active_raids


def calculate_endpoint(uuid, latitude, longitude, request_json, args,
                       deviceworker, deviceschedules, devicesscheduling,
                       devices, geofences, log):
    result = "teleport_gym?oldest_first=true"

    active_devices = get_scheduled_devices(devices, 'findraids', deviceworker["username"])

    if len(active_devices) > 1:
        result += "&raidless=3&no_overlap=true"
    else:
        result += "&raidless=true"

    fences = get_geofences_with_active_raids(geofences, args)
    if fences != "":
        result += "&geofence=" + fences

    return result
