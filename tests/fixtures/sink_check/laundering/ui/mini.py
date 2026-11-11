from fasthtml.common import Script
JS1 = "var link=document.getElementById('a');if(link&&d.url)link.href=d.url;"
JS2 = "var w=document.getElementById('a');w.href=bd.wall_url;"
JS3 = "document.getElementById('a').href=d.url;"
def f(): return Script(JS1), Script(JS2), Script(JS3)
