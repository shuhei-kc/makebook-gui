"""Bounded diagnostics. A log error cannot stop protocol or database work."""
from pathlib import Path
import os,threading

class SafeLog:
    def __init__(self,path,max_bytes=10*1024*1024,backups=3):
        self.path=Path(path);self.max_bytes=max_bytes;self.backups=backups
        self.error=None;self.dropped=0;self.lock=threading.Lock()

    def write(self,text):
        if not text:return 0
        original_length=len(text)
        raw=text.encode('utf-8')
        if len(raw)>self.max_bytes:
            text=raw[-(self.max_bytes-1):].decode('utf-8',errors='ignore')
            self.dropped+=1
        with self.lock:
            try:
                self.path.parent.mkdir(parents=True,exist_ok=True)
                size=self.path.stat().st_size if self.path.exists() else 0
                if size+len(text.encode('utf-8'))>self.max_bytes:
                    for index in range(self.backups,0,-1):
                        source=self.path if index==1 else self.path.with_name(self.path.name+'.'+str(index-1))
                        target=self.path.with_name(self.path.name+'.'+str(index))
                        if source.exists():os.replace(source,target)
                with self.path.open('a',encoding='utf-8',newline='\n') as stream:stream.write(text)
                self.error=None
            except OSError as error:
                self.error=str(error);self.dropped+=1
        return original_length

    def flush(self):pass
    def isatty(self):return False