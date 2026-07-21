import threading, time
from backend.hdencode_coordinator import HDEncodeTrafficCoordinator, HDEncodeTrafficDenied

class Db:
    def get_source_health(self): return {}
    def record_source_success(self,*a,**k): pass
    def record_source_failure(self,*a,**k): pass

def test_user_priority_starts_before_background_waiter():
    c=HDEncodeTrafficCoordinator(); c._MIN_START_INTERVAL=0; c.configure({"hdencode_enabled":True},Db())
    for _ in range(3): c._semaphores["detail"].acquire()
    order=[]
    def run(name,priority):
        with c.request("detail",priority=priority): order.append(name)
    low=threading.Thread(target=run,args=("low",10)); high=threading.Thread(target=run,args=("high",90))
    low.start(); time.sleep(.02); high.start(); time.sleep(.02)
    for _ in range(3): c._semaphores["detail"].release()
    low.join(2); high.join(2)
    assert order[0]=="high"

def test_confirmed_block_denies_queued_background():
    c=HDEncodeTrafficCoordinator(); c._MIN_START_INTERVAL=0; c.configure({"hdencode_enabled":True},Db())
    c._semaphores["rss"].acquire(); result=[]
    def run():
        try:
            with c.request("rss",priority=10): result.append("started")
        except HDEncodeTrafficDenied: result.append("denied")
    thread=threading.Thread(target=run); thread.start(); time.sleep(.02)
    c.observe_http_status(403); c.observe_http_status(403); c.observe_http_status(403)
    c._semaphores["rss"].release(); thread.join(2)
    assert result==["denied"]
