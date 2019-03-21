var rawDataIsLoading = false

// Raw data updating
var minUpdateDelay = 300000 // Minimum delay between updates (in ms).
var lastRawUpdateTime = new Date()

function showQuests(data) {
  $('#myTable tbody > tr').remove();
  console.log("fetch quests");
  if (data != null){
    console.log("got data no position");
    let quests = data['quests'].sort(function(a, b) {
      return b['last_scanned'] - a['last_scanned'];
    });
    $.each( quests, function( key, val ) {
      let imgSrc
      if (val['url'] === '') {
        imgSrc = 'static/images/pokestop/Pokestop_Quest.png'
      } else {
        imgSrc = val['url']
      }

      $( "<tr/>", {
        "class": 'my-new-list',
        "type": val['reward_type'],
        html: "<td><img class='reward-icon' src='static/icons/" + val['icon'] +".png' style='width:100px;height:100px;'></td><td><a target=\"_blank\" href=\"./?lat="+val['latitude']+"&lon="+val['longitude']+"\">" + val['name'] + "</a></br> \
          Quest: "+ val['quest_text']+ "</br> \
          Reward: "+ val['reward_text'] + "</br> \
          </td><td><img class='pokestop img sprite' src='" + imgSrc + "' style='width:100px;height:100px;'></td><td>"+ moment(val['last_scanned']).format('HH:mm') + " - <span class='label-countdown' disappears-at='" + val['expiration'] + "'>00m00s</span> left</td>"
      }).appendTo( "tbody" );
    });
    applyFilter();
  }
}

function loadRawData() {
    return $.ajax({
        url: 'raw_quests',
        type: 'post',
        data: {
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

function updateQuests() {
    lastRawUpdateTime = new Date()
    loadRawData().done(function (result) {
        // Parse result on success.
        showQuests(result)
    }).always(function () {
        // Only queue next request when previous is over.
        // Minimum delay of minUpdateDelay.
        var diff = new Date() - lastRawUpdateTime
        var delay = Math.max(minUpdateDelay - diff, 1) // Don't go below 1.

        // Don't use interval.
        window.setTimeout(updateQuests, delay)
    })
}


/*
 * Document ready
 */
$(document).ready(function () {
  loadRawData().done(function (result) {
    showQuests(result)
    window.setTimeout(updateQuests, minUpdateDelay)
    window.setInterval(updateLabelDiffTime, 1000)
  })

})

function applyFilter() {
    var input, filter, table, tr, td, i;
    input = document.getElementById("quest");
    filter = input.value.toUpperCase();
    table = document.getElementById("myTable");
    tr = table.getElementsByTagName("tr");
    for (i = 0; i < tr.length; i++) {
        td = tr[i].getElementsByTagName("td")[1];
        if (td) {
            if (td.innerHTML.toUpperCase().indexOf(filter) > -1) {
                tr[i].style.display = "";
            } else {
                tr[i].style.display = "none";
            }
        }
    }
}

function lpad(str, len, padstr) {
    return Array(Math.max(len - String(str).length + 1, 0)).join(padstr) + str
}

function getTimeUntil(time) {
    var now = Date.now()
    var tdiff = time - now

    var sec = Math.floor((tdiff / 1000) % 60)
    var min = Math.floor((tdiff / 1000 / 60) % 60)
    var hour = Math.floor((tdiff / (1000 * 60 * 60)) % 24)

    return {
        'total': tdiff,
        'hour': hour,
        'min': min,
        'sec': sec,
        'now': now,
        'ttime': time
    }
}

var updateLabelDiffTime = function () {
    $('.label-countdown').each(function (index, element) {
        var disappearsAt = getTimeUntil(parseInt(element.getAttribute('disappears-at')))

        var hours = disappearsAt.hour
        var minutes = disappearsAt.min
        var seconds = disappearsAt.sec
        var timestring = ''

        if (disappearsAt.ttime < disappearsAt.now) {
            timestring = '(expired)'
        } else {
            timestring = lpad(hours, 2, 0) + ':' + lpad(minutes, 2, 0) + ':' + lpad(seconds, 2, 0)
        }

        $(element).text(timestring)
    })
}
