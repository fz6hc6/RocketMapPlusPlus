;(function () {
    'use strict'

    // addEventsListener
    function addEventsListener(object, types, callback) {
        types.split(' ').forEach(type => object.addEventListener(type, callback))
    }

    // Vars.
    var $body = document.querySelector('body')

    // Nav.
    var $nav = document.querySelector('#nav')
    var $navToggle = document.querySelector('a[href="#nav"]')
    var $navClose

    // Stats.
    var $stats = document.querySelector('#stats')
    var $statsToggle = document.querySelector('a[href="#stats"]')
    var $statsClose

    // find sidebar.
    var $find = document.querySelector('#find')
    var $findToggle = document.querySelector('a[href="#find"]')
    var $findClose

    // Gym sidebar
    var $gymSidebar = document.querySelector('#gym-details')
    var $gymSidebarClose

    // Event: Prevent clicks/taps inside the nav from bubbling.
    addEventsListener($nav, 'click touchend', function (event) {
        event.stopPropagation()
    })

    if ($find) {
        // Event: Prevent clicks/taps inside the find from bubbling.
        addEventsListener($find, 'click touchend', function (event) {
            event.stopPropagation()
        })
    }

    if ($stats) {
        // Event: Prevent clicks/taps inside the stats from bubbling.
        addEventsListener($stats, 'click touchend', function (event) {
            event.stopPropagation()
        })
    }

    if ($gymSidebar) {
        // Event: Prevent clicks/taps inside the gym sidebar from bubbling.
        addEventsListener($gymSidebar, 'click touchend', function (event) {
            event.stopPropagation()
        })
    }

    // Pokestop sidebar
    var $pokestopSidebar = document.querySelector('#pokestop-details')
    var $pokestopSidebarClose

    if ($pokestopSidebar) {
        // Event: Prevent clicks/taps inside the gym sidebar from bubbling.
        addEventsListener($pokestopSidebar, 'click touchend', function (event) {
            event.stopPropagation()
        })
    }

    // Event: Hide nav on body click/tap.
    addEventsListener($body, 'click touchend', function (event) {
        // on ios safari, when navToggle is clicked,
        // this function executes too, so if the target
        // is the toggle button, exit this function
        if (event.target.matches('a[href="#nav"]')) {
            return
        }
        if ($find && event.target.matches('a[href="#find"]')) {
            return
        }
        if ($stats && event.target.matches('a[href="#stats"]')) {
            return
        }
        $nav.classList.remove('visible')
        if ($find) {
            $find.classList.remove('visible')
        }
        if ($stats) {
            $stats.classList.remove('visible')
        }
    })
    // Toggle.

    // Event: Toggle nav on click.
    $navToggle.addEventListener('click', function (event) {
        event.preventDefault()
        event.stopPropagation()
        $nav.classList.toggle('visible')
    })

    // Event: Toggle find on click.
    if ($findToggle) {
        $findToggle.addEventListener('click', function (event) {
            event.preventDefault()
            event.stopPropagation()
	    updateStopsGymsList()
	    updateQuestList()
            $find.classList.toggle('visible')
        })
    }
    // Event: Toggle stats on click.
    if ($statsToggle) {
        $statsToggle.addEventListener('click', function (event) {
            event.preventDefault()
            event.stopPropagation()
            $stats.classList.toggle('visible')
        })
    }

    // Close.

    // Create elements.
    $navClose = document.createElement('a')
    $navClose.href = '#'
    $navClose.className = 'close'
    $navClose.tabIndex = 0
    $nav.appendChild($navClose)

    if ($find) {
        $findClose = document.createElement('a')
        $findClose.href = '#'
        $findClose.className = 'close'
        $findClose.tabIndex = 0
        $find.appendChild($findClose)
    }

    if ($stats) {
        $statsClose = document.createElement('a')
        $statsClose.href = '#'
        $statsClose.className = 'close'
        $statsClose.tabIndex = 0
        $stats.appendChild($statsClose)
    }

    if ($gymSidebar) {
        $gymSidebarClose = document.createElement('a')
        $gymSidebarClose.href = '#'
        $gymSidebarClose.className = 'close'
        $gymSidebarClose.tabIndex = 0
        $gymSidebar.appendChild($gymSidebarClose)
    }

    if ($pokestopSidebar) {
        $pokestopSidebarClose = document.createElement('a')
        $pokestopSidebarClose.href = '#'
        $pokestopSidebarClose.className = 'close'
        $pokestopSidebarClose.tabIndex = 0
        $pokestopSidebar.appendChild($pokestopSidebarClose)
    }

    // Event: Hide on ESC.
    window.addEventListener('keydown', function (event) {
        if (event.keyCode === 27) {
            $nav.classList.remove('visible')
            if ($find) {
                $find.classList.remove('visible')
            }
            if ($stats) {
                $stats.classList.remove('visible')
            }
            if ($gymSidebar) {
                $gymSidebar.classList.remove('visible')
            }
            if ($pokestopSidebar) {
                $pokestopSidebar.classList.remove('visible')
            }
        }
    })

    // Event: Hide nav on click.
    $navClose.addEventListener('click', function (event) {
        event.preventDefault()
        event.stopPropagation()
        $nav.classList.remove('visible')
    })

    if ($findClose) {
        // Event: Hide find on click.
        $findClose.addEventListener('click', function (event) {
            event.preventDefault()
            event.stopPropagation()
            $find.classList.remove('visible')
        })
    }

    if ($statsClose) {
        // Event: Hide stats on click.
        $statsClose.addEventListener('click', function (event) {
            event.preventDefault()
            event.stopPropagation()
            $stats.classList.remove('visible')
        })
    }

    if ($gymSidebarClose) {
        // Event: Hide stats on click.
        $gymSidebarClose.addEventListener('click', function (event) {
            event.preventDefault()
            event.stopPropagation()
            $gymSidebar.classList.remove('visible')
        })
    }

    if ($pokestopSidebarClose) {
        // Event: Hide stats on click.
        $pokestopSidebarClose.addEventListener('click', function (event) {
            event.preventDefault()
            event.stopPropagation()
            $pokestopSidebar.classList.remove('visible')
        })
    }
})()
