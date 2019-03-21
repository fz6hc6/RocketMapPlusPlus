var rawDataIsLoading = false
var statusPageUsername = false
var statusPagePassword = false

// Raw data updating
var minUpdateDelay = 300000 // Minimum delay between updates (in ms).
var lastRawUpdateTime = new Date()
var admin = false

function showDevices(data) {
  $('#myTable tbody > tr').remove();
  console.log("fetch devices");
  if (data != null){
    console.log("got data no position");
    admin = data['admin']
    let devices = data['devices'].sort(function(a, b) {
      if (a['name'].toLowerCase() === b['name'].toLowerCase()){
        return a['deviceid'].toLowerCase().localeCompare(b['deviceid'].toLowerCase());
      } else {
        return a['name'].toLowerCase().localeCompare(b['name'].toLowerCase());
      }
    });

    $.each( devices, function( key, val ) {
      $( "<tr/>", {
        "class": 'my-new-list',
        html: "<td>" + val['deviceid'] + '</td><td><input type="text" value="'+val['name'] + '" id="inputname-' + val['deviceid'] + '"/> <button id="buttonname-' + val['deviceid'] + '" name="changeName-' + val['deviceid'] + '" onclick="changeName(this)">Update Name</button></td><td><input type="text" value="'+val['username'] + '" id="inputusername-' + val['deviceid'] + '"/> <button id="buttonusername-' + val['deviceid'] + '" name="changeUserName-' + val['deviceid'] + '" onclick="changeUserName(this)">Update Username</button></td><td>' + val['pogoversion'] + '</td><td>' + val['scans'] + '</td><td>' + val['scanning'] + "</td><td>" + val['fetching'] + (val['mapcontrolled']?' (Map Controlled)':'') + ' - ' + val['route'] + " Points left in schedule</td><td><a target=\"_blank\" href=\"https://www.google.com/maps/dir/Current+Location/"+val['latitude']+","+val['longitude']+'\">Check on map</a></td><td><input type="text" value="'+val['latitude']+","+val['longitude']+'" id="inputlocation-' + val['deviceid'] + '"/> <button id="buttonlocation-' + val['deviceid'] + '" name="changeLocation-' + val['deviceid'] + '" onclick="changeLocation(this)">Teleport</button></td><td>' + val['requestedEndpoint'] + '</td><td><input type="text" value="'+val['endpoint']+'" id="inputendpoint-' + val['deviceid'] + '"/> <button id="buttonendpoint-' + val['deviceid'] + '" name="changeEndpoint-' + val['deviceid'] + '" onclick="changeEndpoint(this)">Update Endpoint</button></td>'
      }).appendTo( "tbody" );
    });

    if (admin != true) {
        $("button").each(function() {
          if ($(this).attr('id').indexOf('buttonusername') !== -1) {
            document.getElementById($(this).attr('id')).style.display="none";
          }
        });
      }
  }
}

function changeClass(obj){
  $('#' + obj.id).toggleClass('inactive');
}

function changeLocation(obj) {
  var buttonid = obj.id;
  var inputid = buttonid.replace("buttonlocation-", "inputlocation-");
  var uuid = buttonid.replace("buttonlocation-", "");

  $("input[id='" + inputid + "']").each(function () {
    console.log($(this));
    var newcoords = $(this).val();
    console.log(newcoords);
    $.post('next_loc?coords=' + newcoords + '&uuid=' + uuid)
  });
}

function changeName(obj) {
  var buttonid = obj.id;
  var inputid = buttonid.replace("buttonname-", "inputname-");
  var uuid = buttonid.replace("buttonname-", "");

  $("input[id='" + inputid + "']").each(function () {
    console.log($(this));
    var newname = $(this).val();
    console.log(newname);
    $.post('new_name?name=' + newname + '&uuid=' + uuid)
  });
}

function changeUserName(obj) {
  var buttonid = obj.id;
  var inputid = buttonid.replace("buttonusername-", "inputusername-");
  var uuid = buttonid.replace("buttonusername-", "");

  $("input[id='" + inputid + "']").each(function () {
    console.log($(this));
    var newname = $(this).val();
    console.log(newname);
    $.post('new_username?username=' + newname + '&uuid=' + uuid)
  });
}

function changeEndpoint(obj) {
  var buttonid = obj.id;
  var inputid = buttonid.replace("buttonendpoint-", "inputendpoint-");
  var uuid = buttonid.replace("buttonendpoint-", "");

  $("input[id='" + inputid + "']").each(function () {
    console.log($(this));
    var newendpoint = $(this).val();
    newendpoint = newendpoint.replace("?", "||");
    newendpoint = newendpoint.replace(/&/g, "|");
    console.log(newendpoint);
    $.post('new_endpoint?uuid=' + uuid + '&endpoint=' + newendpoint)
  });
}


function loadRawData() {
    return $.ajax({
        url: 'raw_devices',
        type: 'post',
        data: {
            'username': statusPageUsername,
            'password': statusPagePassword,
            'geofencenames': geofencenames
        },
        dataType: 'json',
        beforeSend: function () {
            if (rawDataIsLoading) {
                return false
            } else {
                rawDataIsLoading = true
            }
        },
        complete: function () {
            rawDataIsLoading = false
        }
    })
}

function updateDevices() {
    lastRawUpdateTime = new Date()
    loadRawData().done(function (result) {
        // Parse result on success.
        showDevices(result)
    }).always(function () {
        // Only queue next request when previous is over.
        // Minimum delay of minUpdateDelay.
        var diff = new Date() - lastRawUpdateTime
        var delay = Math.max(minUpdateDelay - diff, 1) // Don't go below 1.

        // Don't use interval.
        window.setTimeout(updateDevices, delay)
    })
}


/*
 * Document ready
 */
$(document).ready(function () {
    // Set focus on username field.
    $('#username').focus()

    // Register to events.
    $('#password_form').submit(function (event) {
        event.preventDefault()

        statusPageUsername = $('#username').val()
        statusPagePassword = $('#password').val()

        loadRawData().done(function (result) {
            if (result.login === 'ok') {
                $('.status_form').remove()
                showDevices(result)
                window.setTimeout(updateDevices, minUpdateDelay)
            } else {
                $('.status_form').effect('bounce')
                $('#username').focus()
            }
        })
    })

    if (needlogin === false) {
      loadRawData().done(function (result) {
        if (result.login === 'ok') {
          $('.status_form').remove()
          showDevices(result)
          window.setTimeout(updateDevices, minUpdateDelay)
        } else {
          $('.status_form').effect('bounce')
          $('#username').focus()
        }
      })
    }

})
