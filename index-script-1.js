
            /* 鍦ㄤ晶鏍?DOM 瑙ｆ瀽瀹屽悗绔嬪嵆鎶?active 绫荤Щ鍒板綋鍓嶉〉瀵瑰簲鐨勫叆鍙ｏ紝閬垮厤棣栧睆鍏堝湪鏂囩敓鍥句笂闂竴涓?*/
            (function(){
                try {
                    var pageId = localStorage.getItem('studio_active_page');
                    if(pageId && pageId !== 'zimage') {
                        document.querySelectorAll('.nav-item.active, .side-pill.active').forEach(function(n){
                            n.classList.remove('active');
                        });
                        var target = document.querySelector('[onclick*="\'' + pageId + '\'"]');
                        if(target) target.classList.add('active');
                    }
                } catch(e) {}
            })();
        