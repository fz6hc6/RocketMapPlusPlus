//
// Stilen from https://stackoverflow.com/questions/1129216/sort-array-of-objects-by-string-property-value
//
function findcompare(a,b) {
  if (a.type + a.name < b.type + b.name)
    return -1;
  if (a.type + a.name > b.type + b.name)
    return 1;
  return 0;
}

function updateQuestList() {
	// Bounds of the currently visible map
    var currentVisibleMap = map.getBounds()

        //var stopGymList  = '<table><tr><th>Name</th></tr>'
        var stopGymList  = '<div class="QuestList">'
	var list = getStops()
	var list = list.sort(findcompare)

	var list = list.filter(function(item) {
            if (item['quest'] === undefined){
		return false
	    }
	    if (item['quest'].toLowerCase().includes(document.getElementById("fp_quest_filter").value.toLowerCase())){
                return true
	    }
	    if (item['name'].toLowerCase().includes(document.getElementById("fp_quest_filter").value.toLowerCase())){
                return true
	    }
	    return false
	});


	$.each(list, function (key, value) {
            var thisPokestopLocation = { lat: list[key]['lat'], lng: list[key]['lng'] }
            if (currentVisibleMap.contains(thisPokestopLocation))
	    {
                //stopGymList += '<tr onmouseover="fp_draw_circle('+ list[key]['lat'] + ', '+ list[key]['lng'] +')" onmouseout="fp_remove_circle()" ondblclick="centerMap('+ list[key]['lat'] +', '+ list[key]['lng'] +', 17)"><td><img src="static/images/' +  list[key]['image'] + '" class="stopgym-image" />' + list[key]['name'] + '</td></tr>'
                stopGymList += '<div class="questtooltip" onmouseover="fp_draw_circle('+ list[key]['lat'] + ', '+ list[key]['lng'] +')" onmouseout="fp_remove_circle()" ondblclick="centerMap('+ list[key]['lat'] +', '+ list[key]['lng'] +', 17)"><td><img src="static/images/' +  list[key]['image'] + '" class="stopgym-image" />' + list[key]['name']
		if (value['quest']){
		    stopGymList += '<span class="questtext">' + value['quest'] + '</span></div><br />'
		}
		else
		{
		    stopGymList += '</div><br />'
		}
            }
	});

	stopGymList += '</div>'

	document.getElementById('findQuests').innerHTML = stopGymList
}

function updateStopsGymsList() {
	// Bounds of the currently visible map
    var currentVisibleMap = map.getBounds()

        //var stopGymList  = '<table><tr><th>Name</th></tr>'
        var stopGymList  = '<div class="gymStopList">'
	var stopList = getStops()
	var gymList = getGyms()
	if (stopList.length >= 1) {
	   var list = stopList.concat(gymList)
	}
	else {
	    var list = gymList
	}
	var list = list.sort(findcompare)

	var list = list.filter(function(item) {
	    return item['name'].toLowerCase().includes(document.getElementById("fp_sg_filter").value.toLowerCase())
	});


	$.each(list, function (key, value) {
            var thisPokestopLocation = { lat: list[key]['lat'], lng: list[key]['lng'] }
            if (currentVisibleMap.contains(thisPokestopLocation))
	    {
                //stopGymList += '<tr onmouseover="fp_draw_circle('+ list[key]['lat'] + ', '+ list[key]['lng'] +')" onmouseout="fp_remove_circle()" ondblclick="centerMap('+ list[key]['lat'] +', '+ list[key]['lng'] +', 17)"><td><img src="static/images/' +  list[key]['image'] + '" class="stopgym-image" />' + list[key]['name'] + '</td></tr>'
                stopGymList += '<div class="questtooltip" onmouseover="fp_draw_circle('+ list[key]['lat'] + ', '+ list[key]['lng'] +')" onmouseout="fp_remove_circle()" ondblclick="centerMap('+ list[key]['lat'] +', '+ list[key]['lng'] +', 17)"><td><img src="static/images/' +  list[key]['image'] + '" class="stopgym-image" />' + list[key]['name']
		if (value['quest']){
		    stopGymList += '<span class="questtext">' + value['quest'] + '</span></div><br />'
		}
		else
		{
		    stopGymList += '</div><br />'
		}
            }
	});

	stopGymList += '</div>'

	document.getElementById('findStopsGyms').innerHTML = stopGymList
}

function getGyms(){
	var list = new Array()

        $.each(mapData.gyms, function (key, value) {
	    var stop = {
	        type : 'gym',
  	        name : value['name'],
	        lat : value['latitude'],
	        lng : value['longitude'],
	        image : construct_gym_icon(value)
	    }
	    list.push(stop)
  	});

	return list
}

function getStops(){
	var list = new Array()

        $.each(mapData.pokestops, function (key, value) {
	    var stop = {
	        type : 'stop',
 	        name : value['name'],
	        lat : value['latitude'],
	        lng : value['longitude'],
	        image : construct_pokestop_icon(value)
	    }
	    if (value['quest']['quest_text'] !== undefined){
                stop['quest']=value['quest']['quest_text']+ '<br />' + value['quest']['reward_text']
	    }
	    list.push(stop)
        });

	return list
}

function fp_draw_circle(lat, lng){
      var center = {lat: lat, lng: lng}
      fp_circled = new google.maps.Circle({
              strokeColor: '#FF8000',
              strokeOpacity: 1,
              strokeWeight: 5,
              fillColor: '#000000',
              fillOpacity: 0.0,
              map: map,
              center: center,
              radius: 70
      });
}


function fp_remove_circle(){
      fp_circled.setMap(null);
}

function construct_pokestop_icon(pokestop){
    var icon = 'Pokestop'
    if (pokestop['lure_expiration'])
    {
        icon += 'Lured'
    }
    if (Boolean(pokestop.pokemon && pokestop.pokemon.length))
    {
        icon += '_Nearby'
    }
    if (Boolean(pokestop.quest && pokestop.quest.type))
    {
        icon += '_Quest'
    }
    return `pokestop/${icon}.png`

}

function construct_gym_icon(gym){
    const hasActiveRaid = gym.raid && gym.raid.end > Date.now()
    const gymInBattle = getGymInBattle(gym)
    const gymExRaidEligible = getGymExRaidEligible(gym)
    const gymOngoingRaid = gym.raid && Date.now() < gym.raid.end && Date.now() > gym.raid.start

    if (gymOngoingRaid)
    {
    	var iconname = `raid/${gymTypes[gym.team_id]}`
        if (gym.raid.pokemon_id && pokemonWithImages.indexOf(gym.raid.pokemon_id) !== -1)
	{
            iconname += `_${gym.raid.pokemon_id}`
            if (gym.raid.form > 0)
            {
                if (gym.raid.form >= 45 && gym.raid.form <= 80)
		{    
                    if(gym.raid.form % 2 == 0)
                    {
                        iconname += 'A'
                    }
                    else
                    {
                        iconname += `_${gym.raid.form}`
                    }
                }
            }
            else
            {
                iconname += `_${gym.raid.level}_unknown`
            }
            if (gymExRaidEligible)
            {
                iconname += '_ExRaidEligible'
            }
            markerImage = `static/images/raid/${iconname}.png`
        }
    }
//  EGGS :)
    else if (gym.raid && gym.raid.end > Date.now())
    {
        if (gym.raid.pokemon_id)
        {
            var iconname = `raid/${gymTypes[gym.team_id]}`
            if (pokemonWithImages.indexOf(gym.raid.pokemon_id) !== -1)
            {
                iconname += `_${gym.raid.pokemon_id}`
                if (gym.raid.form > 0)
                {
                    if (gym.raid.form >= 45 && gym.raid.form <= 80)
                    {
                        if(gym.raid.form % 2 == 0)
                        {
                            iconname += 'A'
                        }
                    }
                    else
                    {
                        iconname += `_${gym.raid.form}`
                    }
                }
            }
            else
            {
                iconname += `_${gym.raid.level}_unknown`
            }
            if (gymExRaidEligible)
            {
                iconname += '_ExRaidEligible'
            }
        }
        else
        {
            var iconname = `raid/${gymTypes[gym.team_id]}_${getGymLevel(gym)}_${gym.raid.level}`
            if (gymInBattle)
            {
                iconname += '_isInBattle'
            }
            if (gymExRaidEligible)
            {
                iconname += '_ExRaidEligible'
            }
        }
    }
// No raid in progress
    else
    {
        var iconname = `gym/${gymTypes[gym.team_id]}_${getGymLevel(gym)}`
        if (gymInBattle)
        {
            iconname += '_isInBattle'
        }
        if (gymExRaidEligible)
        {
            iconname += '_ExRaidEligible'
        }
    }

    return iconname + '.png'
}
