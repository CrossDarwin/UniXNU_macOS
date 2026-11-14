#!/usr/bin/env python3
"""Fault-inject the recorded Darwin BO mapping implementation without a GPU."""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
patch = (ROOT / 'patches/mesa/turnip-cpu-map-rollback.patch').read_text()
after = ''.join(line[1:] for line in patch.splitlines(True)
                if line.startswith((' ', '+')) and not line.startswith('+++'))
start = after.index('static VkResult\ndarwin_cpu_map_failure(')
end = after.index('\nstatic void', start)
implementation = after[start:end]
fixture = r'''
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstdarg>
#include <string>
#include <pthread.h>
#include "NativeAdrenoABI.h"
using kern_return_t=int;using mach_vm_address_t=uint64_t;using mach_vm_size_t=uint64_t;using VkResult=int;
constexpr int KERN_SUCCESS=0,VK_SUCCESS=0,VK_ERROR_MEMORY_MAP_FAILED=-5,VK_ERROR_DEVICE_LOST=-4;
constexpr int kIOReturnNoMemory=1,kIOReturnNoResources=2,kIOReturnNoSpace=3,kIOReturnVMError=4,kIOReturnBadArgument=5;
constexpr uint32_t kIOMapAnywhere=1,kIOMapCopybackCache=2,kIOMapWriteCombineCache=4;
struct darwin_retire_state {pthread_mutex_t lock=PTHREAD_MUTEX_INITIALIZER;int failure=0;};
struct tu_device {int vk=0;void *darwin_retire;};
struct tu_bo {uint64_t darwin_handle=7,size=4096;void *map=nullptr;uint32_t darwin_token=0;bool never_unmap=false;};
struct {int conn=1;struct {uint32_t flags=NativeAdrenoParamsOwnedCoherent;}params;}dw;
static std::string calls;static int lease_error,map_error,unmap_error,release_error,first_errors,attempts;
static uint64_t address,length;static bool leased,mapped,cached;static unsigned options_seen;
static int mach_task_self(){return 1;}
static int vk_errorf(tu_device *,int result,const char *,...){return result;}
static int vk_device_set_lost(int *,const char *,...){return VK_ERROR_DEVICE_LOST;}
#define TU_RMV(...) ((void)0)
static int dw_call(int method,const void *,size_t,void *out,size_t){
 if(method==NativeAdrenoMethodBoCpuMap){calls+='m';++attempts;int e=first_errors-- >0?kIOReturnNoMemory:lease_error;if(e)return e;assert(!leased);leased=true;auto *r=(NativeAdrenoAbiBoCpuMapReply*)out;r->token=3;r->bytes=8192;return 0;}
 assert(method==NativeAdrenoMethodBoCpuUnmap);calls+='l';assert(leased&&!mapped);if(!release_error)leased=false;return release_error;
}
static int IOConnectMapMemory64(int,uint32_t,int,uint64_t *a,uint64_t *n,unsigned options){calls+='v';assert(leased);options_seen=options;if(map_error)return map_error;*a=address;*n=length;mapped=true;return 0;}
static int IOConnectUnmapMemory64(int,uint32_t,int,uint64_t a){calls+='u';assert(mapped&&a==address);if(!unmap_error)mapped=false;return unmap_error;}
static int darwin_reap_locked(tu_device *,darwin_retire_state *){calls+='c';return 0;}
static bool darwin_cache_flush(tu_device *,darwin_retire_state *){calls+='f';bool any=cached;cached=false;return any;}
'''
cases = r'''
static void reset(darwin_retire_state &r){calls.clear();lease_error=map_error=unmap_error=release_error=first_errors=attempts=0;address=0x100000;length=8192;leased=mapped=cached=false;options_seen=0;r.failure=0;}
int main(){
 darwin_retire_state r;tu_device d{0,&r};
 reset(r);{tu_bo b;assert(darwin_bo_map(&d,&b,nullptr)==0);assert(b.map==(void*)address&&b.darwin_token==3&&b.never_unmap);assert(calls=="mv"&&options_seen==(kIOMapAnywhere|kIOMapCopybackCache));}
 reset(r);{tu_bo b;map_error=kIOReturnBadArgument;assert(darwin_bo_map(&d,&b,nullptr)==-5);assert(calls=="mvl"&&!leased&&!mapped&&!b.map&&!b.darwin_token);}
 reset(r);{tu_bo b;length=4095;assert(darwin_bo_map(&d,&b,nullptr)==-5);assert(calls=="mvul"&&!leased&&!mapped&&!b.map&&!b.darwin_token);}
 reset(r);{tu_bo b;address=0;assert(darwin_bo_map(&d,&b,nullptr)==-5);assert(calls=="mvul"&&!leased&&!mapped);}
 reset(r);{tu_bo b;assert(darwin_bo_map(&d,&b,(void*)0x200000)==-5);assert(calls=="mvul"&&!leased&&!mapped);}
 reset(r);{tu_bo b;length=4095;unmap_error=kIOReturnVMError;assert(darwin_bo_map(&d,&b,nullptr)==-4);assert(calls=="mvu"&&leased&&mapped&&b.map==(void*)address&&b.darwin_token==3&&r.failure==-4);}
 reset(r);{tu_bo b;map_error=kIOReturnBadArgument;release_error=kIOReturnVMError;assert(darwin_bo_map(&d,&b,nullptr)==-4);assert(calls=="mvl"&&leased&&!mapped&&!b.map&&b.darwin_token==3&&r.failure==-4);}
 reset(r);{tu_bo b;first_errors=1;cached=true;assert(darwin_bo_map(&d,&b,nullptr)==0);assert(calls=="mcfmv"&&attempts==2&&leased&&mapped);}
 reset(r);{tu_bo b;first_errors=3;cached=true;assert(darwin_bo_map(&d,&b,nullptr)==-5);assert(calls=="mcfm"&&attempts==2&&!leased&&!mapped);}
 reset(r);{tu_bo b;first_errors=1;assert(darwin_bo_map(&d,&b,nullptr)==-5);assert(calls=="mcf"&&attempts==1&&!leased&&!mapped);}
 reset(r);{tu_bo b;dw.params.flags=0;assert(darwin_bo_map(&d,&b,(void*)address)==0);assert(options_seen==kIOMapWriteCombineCache);}
 puts("PASS: 11 CPU map lifecycle cases - rollback order, retained failed cleanup, bounded resource retry and cache policy");
}
'''
with tempfile.TemporaryDirectory(prefix='turnip-cpu-map-') as name:
    work = Path(name)
    source, binary = work / 'test.cpp', work / 'test'
    source.write_text(fixture + implementation + cases)
    subprocess.run([os.environ.get('CXX', 'c++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-pthread',
                    '-I', str(ROOT / 'drivers/Adreno740'), str(source), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
