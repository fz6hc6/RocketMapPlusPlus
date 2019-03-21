var rawDataIsLoading = false

// Raw data updating
var minUpdateDelay = 300000 // Minimum delay between updates (in ms).
var lastRawUpdateTime = new Date()

function showRaids(data) {
  $('#myTable tbody > tr').remove();
  console.log("fetch raids");
  if (data != null){
    var egg = ['', 'egg_normal.png', 'egg_normal.png', 'egg_rare.png','egg_rare.png','egg_legendary.png'];
    console.log("got data no position");
    let raids = data['raids'].sort(function(a, b) {
      return a['end'] - b['end'];
    });
    $.each( raids, function( key, val ) {
      $( "<tr/>", {
        "class": (val['is_ex_raid_eligible']?'exraid':'my-new-list'),
        "level": val['level'],
        html: "<td>" + moment(val['start']).format('HH:mm') + " - " + moment(val['end']).format('HH:mm')  + "</td><td>"+(val['pokemon_id']?"<img src=\"/static/icons/"+ val['pokemon_id']+ ".png\">":"<img src=\"/static/images/raid/" + egg[val['level']] + "\">") + "</td><td><a target=\"_blank\" href=\"./?lat="+val['latitude']+"&lon="+val['longitude']+"\">" + val['name'] + "</a></br>Level "+val['level']+": "+val['pokemon_name']+ (val['move_1']?"("+val['move_1']:'') + (val['move_2']?"/"+val['move_2']+")":'') +"</br>Last scanned: "+ moment(val['last_scanned']).format('HH:mm') + "</td>"
      }).appendTo( "tbody" );
    });
    applyFilter();
  }
}

function loadRawData() {
    return $.ajax({
        url: 'raw_raid',
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

function updateRaids() {
    lastRawUpdateTime = new Date()
    loadRawData().done(function (result) {
        // Parse result on success.
        showRaids(result)
    }).always(function () {
        // Only queue next request when previous is over.
        // Minimum delay of minUpdateDelay.
        var diff = new Date() - lastRawUpdateTime
        var delay = Math.max(minUpdateDelay - diff, 1) // Don't go below 1.

        // Don't use interval.
        window.setTimeout(updateRaids, delay)
    })
}


/*
 * Document ready
 */
$(document).ready(function () {
  loadRawData().done(function (result) {
    load();
    showRaids(result)
    window.setTimeout(updateRaids, minUpdateDelay)
  })
})

function load(){
  $("button[name='level']").each(function () {
    document.getElementById("filter_"+$(this).attr('id')).checked = JSON.parse(localStorage.getItem('filter_'+$(this).attr('id')));
    if(JSON.parse(localStorage.getItem('filter_'+$(this).attr('id')))){
      $(this).removeClass('inactive')
    }
          // $(this).toggleClass('inactive')
  });
}

function changeClass(obj){
  $('#' + obj.id).toggleClass('inactive');

  $("button[name='level']").each(function () {
    console.log($(this));
    if ( ! $(this).hasClass('inactive') ) {
      document.getElementById("filter_"+$(this).attr('id')).checked = true;
    } else {
      document.getElementById("filter_"+$(this).attr('id')).checked = false;
    }

    $("input[name='filterLevel']").each(function () {
      localStorage.setItem($(this).attr('id'), $(this).is(":checked"));
    });
  });
  applyFilter();
}

$("input[name='filterLevel']").change(function () {
  applyFilter();
  $("input[name='filterLevel']").each(function () {
    localStorage.setItem($(this).attr('id'), $(this).is(":checked"));
  });
});

function applyFilter(){
  var classes = [];
  $("input[name='filterLevel']").each(function () {
    if ($(this).is(":checked")) { classes.push($(this).val());}
  });
  var distance = $("select[name='FilterDist']").val();
  if (classes == "") {
    $("#myTable tbody tr").show();
  } else {
    $("#myTable tbody tr").hide();

    $("#myTable tbody tr").each(function () {
      var show = true;
      var row = $(this);
      classes.forEach(function (className) {
        if (row.attr("level") == className) { row.show(); }
      });
    });
  }
  if (! $("button[name='exraid']").hasClass('inactive')) {
    $("#myTable tbody tr").each(function () {
      var show = false;
      var row = $(this);
      if (!row.hasClass('exraid')) { row.hide(); }
    });
  }
}
