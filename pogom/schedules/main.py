import os


def getschedule(mapcontrolled, uuid, latitude, longitude, request_json, args, deviceworker,
                deviceschedules, devicesscheduling, devices, geofences, log):
    schedule = "scan_loc?scheduled=true"

    schedulename = ""
    schedulename = request_json.get('route', "", type=str)
    if schedulename is None or schedulename == "":
        schedulename = uuid

    schedulefilename = ""
    if schedulename != "":
        schedulefilename = os.path.join(
            args.root_path,
            'pogom',
            'schedules',
            schedulename + ".py")
    if schedulefilename == "" or not os.path.isfile(schedulefilename):
        log.warning("No or incorrect schedulename supplied: {}".format(schedulefilename))
    else:
        try:
            from schedulename import calculate_endpoint
            schedule = calculate_endpoint(uuid, latitude, longitude, request_json, args,
                                          deviceworker, deviceschedules, devicesscheduling,
                                          devices, geofences, log)
        except ImportError:
            return schedule

    if 'scheduled=true' not in schedule:
        if '?' in schedule:
            schedule += "&scheduled=true"
        else:
            schedule += "?scheduled=true"

    return schedule
