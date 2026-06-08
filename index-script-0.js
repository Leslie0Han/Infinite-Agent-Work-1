
        (function(){
            try {
                var theme = localStorage.getItem('studio_theme') || localStorage.getItem('canvas_theme') || 'light';
                if(theme === 'dark') {
                    document.documentElement.classList.add('studio-theme-dark');
                    document.documentElement.classList.add('theme-dark');
                }
                document.documentElement.classList.add('studio-route-booting');
                setTimeout(function(){
                    document.documentElement.classList.remove('studio-route-booting');
                }, 1500);
            } catch(e) {}
        })();
    