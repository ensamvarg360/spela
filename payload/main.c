/* Spela helper: a small HTTP server the PC app loads through the console's ELF loader.
 *   GET  /info                 {"version", "owner", "firmware", "model", "mounts": [{"mount", "total", "free"}]}
 *   GET  /ls?p=<dir>           [{"name", "dir", "size"}]; 404 when the folder is missing
 *   GET  /file?p=<file>        the file's bytes
 *   PUT  /file?p=<file>        writes the request body (needs Content-Length) to the file
 *   POST /mkdir?p=<dir>  /rename?p=<from>&to=<to>  /rm?p=<file or empty folder>
 *   POST /quit                 stops the helper, so a newer copy can take the port (a new copy sends it to an old one itself)
 *   POST /uninstall?id=<title ID>  removes a ShadowMountPlus tile whose game files are gone, now or once the console lets it (uninstall_tile())
 *   POST /logo?id=<title ID>   PNG body: the title logo on the game's home-screen page; empty body: none again (set_logo());
 *                              kept on the console and put back after a restart (keep_logos())
 *   POST /copy?p=<from>&to=<to>  starts copying a file or folder on the console, e.g. to another drive (copy_start())
 *   GET  /copy                 {"running", "done", "total", "error", "to"} of that copy;  POST /copy/cancel stops it
 *   POST /pack?p=<game folder>  LZ4-packs the game on the console and puts it in the original's place (pack_start())
 *   GET  /pack                 {"running", "kind", "phase", "done", "total", "before", "after", "game", "backup", "error"};
 *                              POST /pack/cancel stops it
 *   POST /pack/finish?p=<packed game>&keep=1|0  deletes the original set aside after packing, or puts it back (pack_finish())
 *   any  /local/...            from this console only, without the key: the PS5 app's Installed and LZ4 tabs, with the PC away
 *                              (local_games(), local_pack(), and /pack, /pack/cancel, /pack/finish as above)
 *   POST /install?p=<.pkg in a homebrew folder>&cid=<content ID>  starts the console's package installer (install_pkg(), untested)
 *   GET  /klog?s=<seconds>     the kernel log as it's written, for up to 120 seconds: the loader's reason when an app won't start
 *   GET  /relay  POST /relay?id=<id>&status=<status>  the PS5 app's requests, taken and answered by the PC (relay_app())
 *   any  /api/v1/console-app/...  from this console only, without the key: the PS5 app, passed on to the PC
 * Paths are percent-encoded. Every request needs this console's key in X-Storefront-Key (authorized()); even so,
 * writes are refused outside the homebrew folders (writable()) and one title's save data (save_path(), for a restore), and /logo changes only the one file and database column it names (and its kept copy). One thread per connection: /info still answers during a long upload.
 * Build with ./build.ps1; VERSION comes from backend/app/__init__.py. */
#include <dirent.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <time.h>
#include <unistd.h>

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/mount.h>
#include <sys/param.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/time.h>

#include <ps5/kernel.h>

#include <sys/statvfs.h>

#include "sqlite3.h"  /* the amalgamation build.ps1 downloads; compiled in, the console has no SQLite to link */
#include "ampr_pack.h"  /* e2a/: exFAT2AMPR's LZ4 packer, for POST /pack */
#include "verify.h"

#define PORT 7735
#define BLOCK (1 << 20)
#define LOGO_MAX (16 << 20)
#define APP_DB "/system_data/priv/mms/app.db" /* the home screen's database */

typedef struct {
  unsigned long unknown1;
  char str_version[0x1c];
  unsigned int bin_version;
  unsigned long unknown2;
} sce_version_t;

typedef struct {
  char useless1[45];
  char message[3075];
} notify_request_t;

int sceKernelGetProsperoSystemSwVersion(sce_version_t *);
int sceKernelGetHwModelName(char *);
int sceKernelSendNotificationRequest(int, notify_request_t *, size_t, int);

#define SMP_API_PORT 10101                   /* ShadowMountPlus 1.7+ HTTP API, on 127.0.0.1 by default */
#define AUTHID_SHELLCORE 0x4800000000000006ull /* what ShadowMountPlus runs as to use AppInstUtil */

/* The PC app finds the bundled helper's version by searching the .elf for this string. */
__attribute__((used)) static const char VERSION_MARKER[] = "storefront-helper-version:" VERSION;

/* This console's key, which every request must carry. The PC app writes it over the ?s in the copy it sends
 * (helper_elf() in backend/app/consoles.py). volatile: read the patched bytes, never the placeholder folded in. */
#define KEY_PREFIX "storefront-helper-key:"
#define KEY_LEN 64
__attribute__((used)) static volatile const char KEY_SLOT[] =
    KEY_PREFIX "????????????????????????????????????????????????????????????????";

/* Which Spela sent this copy, e.g. "Docker · nas": written over the ?s like the key, ended by a NUL. Told to anyone
 * who asks, even without the key, so another Spela can say who's already connected. Empty when unpatched. */
#define OWNER_PREFIX "storefront-helper-owner:"
#define OWNER_LEN 48
__attribute__((used)) static volatile const char OWNER_SLOT[] =
    OWNER_PREFIX "????????????????????????????????????????????????";
static char owner[OWNER_LEN];

static void read_owner(void) {
  const volatile char *s = OWNER_SLOT + sizeof OWNER_PREFIX - 1;
  for (int i = 0; s[0] != '?' && i < OWNER_LEN - 1 && s[i]; i++) owner[i] = s[i];
}

typedef struct {
  char *s;
  size_t len, cap;
} buf_t;

static void put(buf_t *b, const char *fmt, ...) {
  for (;;) {
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(b->s + b->len, b->cap - b->len, fmt, ap);
    va_end(ap);
    if (n < 0) return;
    if (b->len + n < b->cap) {
      b->len += n;
      return;
    }
    char *s = realloc(b->s, (b->cap + n) * 2);
    if (!s) return;
    b->s = s;
    b->cap = (b->cap + n) * 2;
  }
}

static void put_str(buf_t *b, const char *s) {
  put(b, "\"");
  for (; *s; s++) {
    if (*s == '"' || *s == '\\') put(b, "\\%c", *s);
    else if ((unsigned char)*s < 0x20) put(b, "\\u%04x", *s);
    else put(b, "%c", *s);
  }
  put(b, "\"");
}

static int send_all(int c, const char *p, size_t n) {
  while (n) {
    ssize_t r = send(c, p, n, 0);
    if (r <= 0) return -1;
    p += r;
    n -= r;
  }
  return 0;
}

static void reply(int c, const char *status, buf_t *b) {
  char head[256];
  int n = snprintf(head, sizeof head,
                   "HTTP/1.1 %s\r\nContent-Type: application/json\r\nContent-Length: %zu\r\nConnection: close\r\n\r\n",
                   status, b->len);
  send_all(c, head, n);
  send_all(c, b->s, b->len);
}

static void fail(int c, const char *status, const char *msg) {
  buf_t b = {0};
  put(&b, "{\"error\":");
  put_str(&b, msg);
  put(&b, "}");
  reply(c, status, &b);
  free(b.s);
}

static void ok(int c) {
  buf_t b = {0};
  put(&b, "{\"ok\":true}");
  reply(c, "200 OK", &b);
  free(b.s);
}

/* Streams /dev/klog for `seconds`. Read-only; it only sees lines written while it runs. */
static void klog(int c, int seconds) {
  int fd = open("/dev/klog", O_RDONLY | O_NONBLOCK);
  if (fd < 0) {
    fail(c, "500 Internal Server Error", strerror(errno));
    return;
  }
  const char *head = "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n";
  char chunk[4096];
  time_t end = time(NULL) + seconds;
  if (send_all(c, head, strlen(head))) end = 0;
  while (time(NULL) < end) {
    ssize_t r = read(fd, chunk, sizeof chunk);
    if (r > 0 ? send_all(c, chunk, r) : 0) break;  /* the PC hung up */
    if (r <= 0) usleep(100000);
  }
  close(fd);
}

static void info(buf_t *b) {
  sce_version_t v;
  char model[1000] = "";
  const char *fw = "";
  memset(&v, 0, sizeof v);
  if (!sceKernelGetProsperoSystemSwVersion(&v)) for (fw = v.str_version; *fw == ' '; fw++);
  sceKernelGetHwModelName(model);

  put(b, "{\"version\":");
  put_str(b, VERSION);
  put(b, ",\"owner\":");
  put_str(b, owner);
  put(b, ",\"firmware\":");
  put_str(b, fw);
  put(b, ",\"model\":");
  put_str(b, model);
  put(b, ",\"mounts\":[");
  int n = getfsstat(NULL, 0, MNT_NOWAIT);
  struct statfs *m = n > 0 ? calloc(n, sizeof *m) : NULL;
  if (m) n = getfsstat(m, n * sizeof *m, MNT_NOWAIT);
  for (int i = 0, first = 1; m && i < n; i++) {
    if (!strncmp(m[i].f_mntonname, "/mnt/sandbox/", 13)) continue;  /* hundreds of per-app sandboxes, not drives */
    put(b, first ? "{\"mount\":" : ",{\"mount\":");
    first = 0;
    put_str(b, m[i].f_mntonname);
    put(b, ",\"total\":%llu,\"free\":%llu}", (unsigned long long)m[i].f_blocks * m[i].f_bsize,
        (unsigned long long)m[i].f_bavail * m[i].f_bsize);
  }
  free(m);
  put(b, "]}");
}

static int hexval(char h) {
  return h >= '0' && h <= '9' ? h - '0' : h >= 'a' && h <= 'f' ? h - 'a' + 10 : h >= 'A' && h <= 'F' ? h - 'A' + 10 : -1;
}

/* Decodes query parameter `key` into `out`; 1 when it was there and fit, else 0. */
static int param(const char *q, const char *key, char *out, size_t n) {
  size_t k = strlen(key), o = 0;
  for (; q; q = strchr(q, '&') ? strchr(q, '&') + 1 : NULL) {
    if (strncmp(q, key, k) || q[k] != '=') continue;
    for (q += k + 1; *q && *q != '&'; q++) {
      int ch = *q;
      if (ch == '%') {
        int hi = hexval(q[1]), lo = hi < 0 ? -1 : hexval(q[2]);
        if (lo < 0 || !(ch = hi * 16 + lo)) return 0;
        q += 2;
      }
      if (o == n - 1) return 0;
      out[o++] = ch;
    }
    out[o] = 0;
    return 1;
  }
  return 0;
}

static int dotdot(const char *p) {
  for (const char *s = p; (s = strstr(s, "/..")); s += 3)
    if (!s[3] || s[3] == '/') return 1;
  return 0;
}

/* Only inside /data/homebrew, /mnt/usbN/homebrew or /mnt/extN/homebrew (or that folder itself), and no "..". */
static int writable(const char *p) {
  const char *rest = NULL;
  if (!strncmp(p, "/data/", 6)) rest = p + 5;
  else if ((!strncmp(p, "/mnt/usb", 8) || !strncmp(p, "/mnt/ext", 8)) && p[8] >= '0' && p[8] <= '9') rest = p + 9;
  if (!rest || strncmp(rest, "/homebrew", 9) || (rest[9] && rest[9] != '/')) return 0;
  return !dotdot(p);
}

/* One title's save data, for a restore: /user/home/<user>/<save folder>/<entry naming a title ID, such as PPSA01234
 * or .PPSA01234.partial> or anything inside that entry, and no "..". The user and save folders themselves never. */
static int save_path(const char *p) {
  if (strncmp(p, "/user/home/", 11) || dotdot(p)) return 0;
  const char *kind = strchr(p + 11, '/'), *entry = kind ? strchr(kind + 1, '/') : NULL;
  if (!kind || kind == p + 11 || !entry || entry == kind + 1 || !entry[1]) return 0;
  const char *end = strchr(entry + 1, '/');
  if (!end) end = entry + strlen(entry);
  for (const char *s = entry + 1; s + 9 <= end; s++)  /* [A-Z]{4}[0-9]{5} */
    if (strspn(s, "ABCDEFGHIJKLMNOPQRSTUVWXYZ") >= 4 && strspn(s + 4, "0123456789") >= 5) return 1;
  return 0;
}

static void list(int c, const char *path) {
  DIR *d = opendir(path);
  if (!d) {
    fail(c, "404 Not Found", strerror(errno));
    return;
  }
  buf_t b = {0};
  char full[PATH_MAX];
  struct dirent *e;
  int first = 1;
  put(&b, "[");
  while ((e = readdir(d))) {
    struct stat st;
    if (!strcmp(e->d_name, ".") || !strcmp(e->d_name, "..")) continue;
    if (snprintf(full, sizeof full, "%s/%s", path, e->d_name) >= (int)sizeof full) continue;
    if (lstat(full, &st)) continue;  /* lstat: a symlink is listed as a file, never followed */
    put(&b, first ? "{\"name\":" : ",{\"name\":");
    first = 0;
    put_str(&b, e->d_name);
    put(&b, ",\"dir\":%s,\"size\":%lld}", S_ISDIR(st.st_mode) ? "true" : "false", (long long)st.st_size);
  }
  closedir(d);
  put(&b, "]");
  reply(c, "200 OK", &b);
  free(b.s);
}

static void download(int c, const char *path) {
  struct stat st;
  int fd = open(path, O_RDONLY);
  if (fd < 0 || fstat(fd, &st) || !S_ISREG(st.st_mode)) {
    if (fd >= 0) close(fd);
    fail(c, "404 Not Found", "not a file");
    return;
  }
  char head[256], *chunk = malloc(BLOCK);
  int n = snprintf(head, sizeof head,
                   "HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Length: %lld\r\nConnection: close\r\n\r\n",
                   (long long)st.st_size);
  ssize_t r;
  if (chunk && !send_all(c, head, n))
    while ((r = read(fd, chunk, BLOCK)) > 0 && !send_all(c, chunk, r));
  free(chunk);
  close(fd);
}

/* The request's Content-Length, or -1 without one. */
static long long content_length(const char *head) {
  const char *cl = strstr(head, "\r\nContent-Length:");  /* the PC app is the only client; it sends this spelling */
  return cl ? (long long)strtoull(cl + 17, NULL, 10) : -1;
}

/* Writes the request body (`left` bytes) to `path`. `body`/`have`: the part of it that arrived with the headers.
 * NULL when it's all written, else what went wrong. After a failure the rest of the body is still read and dropped:
 * replying and closing with it unread makes the PC's connection reset (WinError 10053) instead of showing the reason. */
static const char *receive(int c, const char *path, unsigned long long left, const char *body, size_t have) {
  /* 0777: the PS5 won't start an eboot.bin or load a .prx without the execute bit (the spawn fails with EACCES,
   * shown as CE-107750-0). Overwriting keeps a file's mode; a new file would otherwise get none. */
  int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0777);
  char *chunk = malloc(BLOCK);
  const char *err = fd < 0 ? strerror(errno) : chunk ? NULL : "out of memory";
  if (have > left) have = left;
  for (const char *p = body; chunk && left;) {
    if (!have) {
      ssize_t r = recv(c, chunk, left < BLOCK ? left : BLOCK, 0);
      if (r <= 0) {
        if (!err) err = "upload cut off";
        break;
      }
      p = chunk;
      have = r;
    }
    if (!err && write(fd, p, have) != (ssize_t)have) err = strerror(errno);
    left -= have;
    have = 0;
  }
  free(chunk);
  if (fd >= 0 && close(fd) && !err) err = strerror(errno);
  return err;
}

static void upload(int c, const char *path, const char *head, const char *body, size_t have) {
  long long len = content_length(head);
  const char *err = len < 0 ? NULL : receive(c, path, len, body, have);
  if (len < 0) fail(c, "411 Length Required", "Content-Length missing");
  else if (err) fail(c, "500 Internal Server Error", err);
  else ok(c);
}

/* 4 capital letters, then 5 digits; `id` may go on after them. */
static int title_id_at(const char *id) {
  for (int i = 0; i < 9; i++)
    if (i < 4 ? id[i] < 'A' || id[i] > 'Z' : id[i] < '0' || id[i] > '9') return 0;
  return 1;
}

/* Query parameter "id" as a title ID into `id` (16 bytes); 0 when it isn't one. */
static int title_id(const char *query, char *id) {
  return query && param(query, "id", id, 16) && strlen(id) == 9 && title_id_at(id);
}

/* Asks ShadowMountPlus's API to uninstall `id`. Returns the HTTP status (its body in `out`), or 0 when nothing
 * answers on the port: an older ShadowMountPlus, or its API turned off. */
static int smp_uninstall(const char *id, char *out, size_t n) {
  int s = socket(AF_INET, SOCK_STREAM, 0);
  struct sockaddr_in addr;
  struct timeval timeout = {20, 0};
  memset(&addr, 0, sizeof addr);
  addr.sin_len = sizeof addr;
  addr.sin_family = AF_INET;
  addr.sin_port = htons(SMP_API_PORT);
  addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof timeout);
  if (connect(s, (struct sockaddr *)&addr, sizeof addr)) {
    close(s);
    return 0;
  }
  char req[512], resp[4096];
  char body[64];
  int blen = snprintf(body, sizeof body, "{\"title_id\":\"%s\"}", id);
  int rlen = snprintf(req, sizeof req,
                      "POST /api/v1/games/uninstall HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                      "Content-Length: %d\r\nConnection: close\r\n\r\n%s", blen, body);
  size_t got = 0;
  ssize_t r;
  if (!send_all(s, req, rlen))
    while (got < sizeof resp - 1 && (r = recv(s, resp + got, sizeof resp - 1 - got, 0)) > 0) got += r;
  close(s);
  resp[got] = 0;
  int status = 0;
  char *b = strstr(resp, "\r\n\r\n");
  if (sscanf(resp, "HTTP/%*s %d", &status) != 1 || !b) return 502;
  snprintf(out, n, "%s", b + 4);
  return status;
}

/* The console's own installer library (the one ShadowMountPlus uses): opens it, takes ShellCore's authority and
 * initialises it on first use, so a console without it still runs every other endpoint. Returns `name`, or NULL. */
static void *appinst_sym(const char *name) {
  static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
  static void *lib = NULL;
  static int tried = 0;
  pthread_mutex_lock(&lock);
  if (!tried) {
    tried = 1;
    lib = dlopen("/system/common/lib/libSceAppInstUtil.sprx", RTLD_LAZY);
    int (*init)(void) = lib ? (int (*)(void))dlsym(lib, "sceAppInstUtilInitialize") : NULL;
    if (init) {
      kernel_set_ucred_authid(-1, AUTHID_SHELLCORE);
      init();
    } else {
      lib = NULL;
    }
  }
  pthread_mutex_unlock(&lock);
  return lib ? dlsym(lib, name) : NULL;
}

/* Without ShadowMountPlus's API: the console's own uninstall, the call ShadowMountPlus makes itself. */
static int appinstutil_uninstall(const char *id) {
  int (*uninstall)(const char *) = (int (*)(const char *))appinst_sym("sceAppInstUtilAppUnInstall");
  return uninstall ? uninstall(id) : -1;
}

/* Installs the package at `path` (already on the console, inside a homebrew folder) through the console's installer.
 * UNTESTED on a console: the struct layouts follow what other installers pass to sceAppInstUtilInstallByPackage
 * (six string pointers, then a zeroed play-go block), and it needs a package the console will run (fake packages
 * work up to firmware 11.60). The call only starts the install; the PC app watches /user/app/<title ID> for it. */
static void install_pkg(int c, const char *path, const char *query) {
  char cid[64];
  struct stat st;
  if (!param(query, "cid", cid, sizeof cid) || strlen(cid) != 36 || strspn(cid, "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-") != 36) {
    fail(c, "400 Bad Request", "cid must be a content ID");
    return;
  }
  if (stat(path, &st) || !S_ISREG(st.st_mode)) {
    fail(c, "404 Not Found", "not a file");
    return;
  }
  int (*install)(void *, void *) = (int (*)(void *, void *))appinst_sym("sceAppInstUtilInstallByPackage");
  if (!install) {
    fail(c, "501 Not Implemented", "this firmware has no package installer");
    return;
  }
  struct {
    const char *uri, *ex_uri, *playgo_scenario_id, *content_id, *content_name, *icon_url;
  } meta = {path, "", "", cid, cid, ""};
  static char playgo[4096];  /* zeroed, and larger than any layout of it */
  int r = install(&meta, playgo);
  if (r) {
    char msg[64];
    snprintf(msg, sizeof msg, "the console refused (0x%08X)", (unsigned)r);
    fail(c, "500 Internal Server Error", msg);
  } else {
    ok(c);
  }
}

/* The string value of `key` in a flat JSON object, into `out`; 0 when it isn't there. An escape keeps just the
 * character after the backslash: enough for an error message. */
static int json_str(const char *json, const char *key, char *out, size_t n) {
  char pat[64];
  snprintf(pat, sizeof pat, "\"%s\"", key);
  const char *p = strstr(json, pat);
  if (!p || !n) return 0;
  p += strlen(pat);
  while (*p == ' ' || *p == ':') p++;
  if (*p++ != '"') return 0;
  size_t i = 0;
  for (; *p && *p != '"' && i < n - 1; p++) {
    if (*p == '\\' && p[1]) p++;
    out[i++] = *p;
  }
  out[i] = 0;
  return 1;
}

enum { TILE_GONE, TILE_REFUSED, TILE_GAME_THERE };

/* Removes the home-screen tile of a ShadowMountPlus game whose files were deleted. Only such tiles: the title must
 * have ShadowMountPlus's /user/app/<id>/mount.lnk, and the source it names (mount_img.lnk for a disc image) must be
 * gone, so nothing on the network can remove a game that's still there. No tile at all counts as gone. When both
 * ShadowMountPlus and the console refuse, `why` (n bytes) says what each answered. */
static int remove_tile(const char *id, char *why, size_t n) {
  char lnk[PATH_MAX], source[PATH_MAX] = "", msg[4096], smp[512] = "";
  FILE *f = NULL;
  snprintf(lnk, sizeof lnk, "/user/app/%s/mount_img.lnk", id);
  if (!(f = fopen(lnk, "r"))) {
    snprintf(lnk, sizeof lnk, "/user/app/%s/mount.lnk", id);
    f = fopen(lnk, "r");
  }
  if (!f) return TILE_GONE;  /* not a ShadowMountPlus title, or its tile is already gone */
  if (!fgets(source, sizeof source, f)) source[0] = 0;
  fclose(f);
  source[strcspn(source, "\r\n")] = 0;
  struct stat st;
  if (!source[0] || !lstat(source, &st)) return TILE_GAME_THERE;
  int status = smp_uninstall(id, msg, sizeof msg);
  if (status == 200) return TILE_GONE;
  /* ShadowMountPlus missing, or refusing (it may not list a game whose files are gone): the console's own uninstall */
  int r = appinstutil_uninstall(id);
  if (!r) return TILE_GONE;
  if (status && !json_str(msg, "error", smp, sizeof smp)) snprintf(smp, sizeof smp, "HTTP %d", status);
  if (status) snprintf(why, n, "ShadowMountPlus: %s; the console: 0x%08X", smp, (unsigned)r);
  else snprintf(why, n, "the console refused (0x%08X)", (unsigned)r);
  return TILE_REFUSED;
}

/* Tiles that couldn't be removed yet: ShadowMountPlus refuses while a game or app is open, and the PS5 app removing a
 * game is one. retry_tiles() tries again until it works, so the tile goes once the app is closed.
 * ponytail: in memory only, a reboot or helper update forgets them (the tile then stays until deleted by hand). */
#define PENDING_MAX 32
#define PENDING_SECONDS (24 * 60 * 60)
static pthread_mutex_t pending_lock = PTHREAD_MUTEX_INITIALIZER;
static struct {
  char id[16];
  time_t since;
} pending[PENDING_MAX];
static int pending_n;

/* Adds `id` to the tiles to retry; 0 when the list is full. */
static int pend_tile(const char *id) {
  pthread_mutex_lock(&pending_lock);
  int i = 0;
  while (i < pending_n && strcmp(pending[i].id, id)) i++;
  if (i == pending_n && i < PENDING_MAX) {
    snprintf(pending[i].id, sizeof pending[i].id, "%s", id);
    pending[i].since = time(NULL);
    pending_n++;
  }
  pthread_mutex_unlock(&pending_lock);
  return i < PENDING_MAX;
}

static void unpend_tile(const char *id) {
  pthread_mutex_lock(&pending_lock);
  for (int i = 0; i < pending_n; i++)
    if (!strcmp(pending[i].id, id)) pending[i--] = pending[--pending_n];
  pthread_mutex_unlock(&pending_lock);
}

static void *retry_tiles(void *arg) {
  (void)arg;
  for (;;) {
    sleep(10);
    char ids[PENDING_MAX][16], why[1024];
    time_t since[PENDING_MAX];
    pthread_mutex_lock(&pending_lock);
    int n = pending_n;
    for (int i = 0; i < n; i++) {
      memcpy(ids[i], pending[i].id, sizeof ids[i]);
      since[i] = pending[i].since;
    }
    pthread_mutex_unlock(&pending_lock);
    for (int i = 0; i < n; i++)  /* the files check runs again: a game installed since keeps its tile */
      if (remove_tile(ids[i], why, sizeof why) != TILE_REFUSED || time(NULL) - since[i] > PENDING_SECONDS)
        unpend_tile(ids[i]);
  }
  return NULL;
}

/* POST /uninstall: 200 when the tile is gone, 202 {"error", "pending": true} when it goes once the console lets it. */
static void uninstall_tile(int c, const char *query) {
  char id[16] = "", why[1024] = "";
  if (!title_id(query, id)) {
    fail(c, "400 Bad Request", "id must be a title ID");
    return;
  }
  int r = remove_tile(id, why, sizeof why);
  if (r == TILE_GONE) {
    unpend_tile(id);
    ok(c);
  } else if (r == TILE_GAME_THERE) {
    fail(c, "409 Conflict", "the game's files are still on the console");
  } else if (!pend_tile(id)) {
    fail(c, "500 Internal Server Error", why);
  } else {
    buf_t b = {0};
    put(&b, "{\"error\":");
    put_str(&b, why);
    put(&b, ",\"pending\":true}");
    reply(c, "202 Accepted", &b);
    free(b.s);
  }
}

static int is_dir(const char *path) {
  struct stat st;
  return !lstat(path, &st) && S_ISDIR(st.st_mode);
}

/* One copy on the console at a time (POST /copy), so a game moves between drives without crossing the network. */
static pthread_mutex_t copy_lock = PTHREAD_MUTEX_INITIALIZER;
static struct {
  int running;
  volatile int cancel;
  long long done, total;
  char src[PATH_MAX], dst[PATH_MAX], error[256];
} cp;

/* Bytes in the regular files under `path` (or of `path` itself). */
static long long tree_size(const char *path) {
  struct stat st;
  if (lstat(path, &st)) return 0;
  if (!S_ISDIR(st.st_mode)) return S_ISREG(st.st_mode) ? st.st_size : 0;
  long long n = 0;
  char full[PATH_MAX];
  struct dirent *e;
  DIR *d = opendir(path);
  while (d && (e = readdir(d))) {
    if (!strcmp(e->d_name, ".") || !strcmp(e->d_name, "..")) continue;
    if (snprintf(full, sizeof full, "%s/%s", path, e->d_name) < (int)sizeof full) n += tree_size(full);
  }
  if (d) closedir(d);
  return n;
}

/* Copies a file or folder tree to `dst`, which must not exist. NULL when done, else what went wrong. Symlinks and other
 * special files are refused, never followed. */
static const char *copy_tree(const char *src, const char *dst, char *chunk) {
  struct stat st;
  if (lstat(src, &st)) return strerror(errno);
  if (S_ISDIR(st.st_mode)) {
    if (mkdir(dst, 0777)) return strerror(errno);
    DIR *d = opendir(src);
    if (!d) return strerror(errno);
    const char *err = NULL;
    char s[PATH_MAX], t[PATH_MAX];
    struct dirent *e;
    while (!err && (e = readdir(d))) {
      if (!strcmp(e->d_name, ".") || !strcmp(e->d_name, "..")) continue;
      if (snprintf(s, sizeof s, "%s/%s", src, e->d_name) >= (int)sizeof s ||
          snprintf(t, sizeof t, "%s/%s", dst, e->d_name) >= (int)sizeof t) err = "path too long";
      else err = copy_tree(s, t, chunk);
    }
    closedir(d);
    return err;
  }
  if (!S_ISREG(st.st_mode)) return "not a file or folder";
  int in = open(src, O_RDONLY), out = in < 0 ? -1 : open(dst, O_WRONLY | O_CREAT | O_EXCL, st.st_mode & 0777);  /* keeps the execute bit (receive()) */
  const char *err = in < 0 || out < 0 ? strerror(errno) : NULL;
  ssize_t r;
  while (!err && (r = read(in, chunk, BLOCK)) != 0) {
    errno = 0;
    if (r < 0) err = strerror(errno);
    else if (write(out, chunk, r) != r) err = errno ? strerror(errno) : "write cut short";
    else if (cp.cancel) err = "canceled";
    else {
      pthread_mutex_lock(&copy_lock);
      cp.done += r;
      pthread_mutex_unlock(&copy_lock);
    }
  }
  if (in >= 0) close(in);
  if (out >= 0 && close(out) && !err) err = strerror(errno);
  return err;
}

static void *copy_thread(void *arg) {
  (void)arg;
  long long total = tree_size(cp.src);
  pthread_mutex_lock(&copy_lock);
  cp.total = total;
  pthread_mutex_unlock(&copy_lock);
  char *chunk = malloc(BLOCK);
  const char *err = chunk ? copy_tree(cp.src, cp.dst, chunk) : "out of memory";
  free(chunk);
  pthread_mutex_lock(&copy_lock);
  snprintf(cp.error, sizeof cp.error, "%s", err ? err : "");
  cp.running = 0;
  pthread_mutex_unlock(&copy_lock);
  return NULL;
}

/* Starts copying `path` to the query's `to`; both inside the homebrew folders. The copy runs on after the reply:
 * GET /copy reports it. A failed or canceled copy leaves what it wrote, for the PC app to delete. */
static void copy_start(int c, const char *path, const char *query) {
  char to[PATH_MAX];
  struct stat st;
  if (!param(query, "to", to, sizeof to) || !writable(to)) {
    fail(c, "403 Forbidden", "only the homebrew folders can be changed");
    return;
  }
  if (!lstat(to, &st)) {
    fail(c, "409 Conflict", "the destination already exists");
    return;
  }
  pthread_mutex_lock(&copy_lock);
  int busy = cp.running;
  if (!busy) {
    cp.running = 1;
    cp.cancel = 0;
    cp.done = cp.total = 0;
    cp.error[0] = 0;
    snprintf(cp.src, sizeof cp.src, "%s", path);
    snprintf(cp.dst, sizeof cp.dst, "%s", to);
  }
  pthread_mutex_unlock(&copy_lock);
  if (busy) {
    fail(c, "409 Conflict", "another copy is running");
    return;
  }
  pthread_t t;
  if (pthread_create(&t, NULL, copy_thread, NULL)) {
    pthread_mutex_lock(&copy_lock);
    cp.running = 0;
    pthread_mutex_unlock(&copy_lock);
    fail(c, "500 Internal Server Error", "can't start the copy");
    return;
  }
  pthread_detach(t);
  ok(c);
}

static void copy_status(int c) {
  buf_t b = {0};
  pthread_mutex_lock(&copy_lock);
  put(&b, "{\"running\":%s,\"done\":%lld,\"total\":%lld,\"error\":", cp.running ? "true" : "false", cp.done, cp.total);
  put_str(&b, cp.error);
  put(&b, ",\"to\":");
  put_str(&b, cp.dst);
  pthread_mutex_unlock(&copy_lock);
  put(&b, "}");
  reply(c, "200 OK", &b);
  free(b.s);
}

/* LZ4 packing on the console (POST /pack), with exFAT2AMPR's packer in e2a/ (by V-10, GPL-3.0-or-later with
 * additional terms: e2a/NOTICE). The game's assets go into ampr_assets-NNN.pak volumes and fakelib/libSceAmpr.sprx
 * unpacks them as the game reads them, as the PC app's lz4pack.py does. It runs on with the PC away: packed into a
 * hidden .<name>.lz4.partial next to the game, every block checked, then the original is set aside as
 * .<name>.lz4-original (ShadowMountPlus skips hidden names, and two copies of one title ID would clash), its tile
 * removed, and the packed copy renamed into the original's place, so ShadowMountPlus adds it like a new install.
 * The PC app deletes the set-aside original once the packed game has started, or puts it back. One at a time. */
extern const unsigned char libSceAmpr_sprx[];
extern const unsigned int libSceAmpr_sprx_len;
#define SMP_SPRX "/data/shadowmount/emus/libSceAmpr.sprx" /* ShadowMountPlus keeps this one up to date: preferred */
#define PACK_WORKERS 6 /* compression threads, LZ4 level 9: exFAT2AMPR's defaults */
#define PACK_LEVEL 9

/* e2a/activity.h: exFAT2AMPR slows down while a game runs. ponytail: the helper packs at full speed; add its game
 * mode if people play while packing. */
int activity_workers(int requested) { return requested; }
void activity_throttle(uint64_t bytes) { (void)bytes; }

static pthread_mutex_t pack_lock = PTHREAD_MUTEX_INITIALIZER;
static atomic_int pack_cancel;
static struct {
  int running;
  char kind[8];    /* pack, delete (the set-aside original) or restore (it, in place of the packed copy) */
  char phase[16];  /* pack: checking, packing, copying, verifying, waiting, swapping, then done, failed or canceled;
                    * delete: deleting, then deleted or failed; restore: waiting, restoring, then restored or failed */
  unsigned long long done, total, before, after;
  char game[PATH_MAX], backup[PATH_MAX], error[256];
} pk;

static void pack_set(const char *phase, unsigned long long total) {
  pthread_mutex_lock(&pack_lock);
  snprintf(pk.phase, sizeof pk.phase, "%s", phase);
  pk.done = 0;
  pk.total = total;
  pthread_mutex_unlock(&pack_lock);
}

static void pack_progress(void *ctx, uint64_t done, const char *msg) {
  (void)ctx;
  (void)msg;
  pthread_mutex_lock(&pack_lock);
  pk.done = done;
  pthread_mutex_unlock(&pack_lock);
}

static unsigned long long dir_bytes(const char *path) { return (unsigned long long)tree_size(path); }

/* Writes the emulator into <dir>/fakelib and lists it in `l` with its real size, as exFAT2AMPR's job does. */
static const char *pack_runtime(const char *dir, e2a_list *l) {
  char fakelib[PATH_MAX], sprx[PATH_MAX];
  unsigned char magic = 0;
  struct stat st;
  snprintf(fakelib, sizeof fakelib, "%s/fakelib", dir);
  snprintf(sprx, sizeof sprx, "%s/libSceAmpr.sprx", fakelib);
  if (e2a_mkdir_p(fakelib)) return "can't create fakelib";
  int fd = open(SMP_SPRX, O_RDONLY);
  int smp = fd >= 0 && read(fd, &magic, 1) == 1 && magic == 0x4F;  /* a PRX starts with 0x4F */
  if (fd >= 0) close(fd);
  if (!(smp && !e2a_copy_file(SMP_SPRX, sprx, time(NULL))) &&
      e2a_write_file_atomic(sprx, libSceAmpr_sprx, libSceAmpr_sprx_len))
    return "can't write fakelib/libSceAmpr.sprx";
  if (stat(sprx, &st)) return "fakelib/libSceAmpr.sprx is missing";
  for (size_t i = 0; i < l->n; i++)
    if (!strcasecmp(l->v[i].rel, "fakelib/libSceAmpr.sprx")) {  /* the game had its own: ours replaces it */
      l->v[i].synthetic = true;
      l->v[i].action = E2A_LOOSE;
      l->v[i].size = st.st_size;
      l->v[i].mtime = st.st_mtime;
      return NULL;
    }
  return e2a_list_add(l, "fakelib/libSceAmpr.sprx", st.st_size, st.st_mtime, true) ? "out of memory" : NULL;
}

/* remove_tile(), waiting while ShadowMountPlus refuses because the game is open: half an hour at most. */
static int remove_tile_waiting(const char *id) {
  char why[1024];
  int r = TILE_REFUSED;
  for (int tries = 0; tries < 180 && (r = remove_tile(id, why, sizeof why)) == TILE_REFUSED; tries++) {
    pack_set("waiting", 0);
    sleep(10);
  }
  return r;
}

/* Sets the original aside, removes its tile and puts the packed copy in its place. NULL when done. */
static const char *pack_swap(const char *game, const char *work, const char *backup, const char *id) {
  if (rename(game, backup)) return strerror(errno);
  int r = remove_tile_waiting(id);
  if (r == TILE_REFUSED || rename(work, game)) {
    rename(backup, game);
    return r == TILE_REFUSED ? "the game's tile couldn't be removed: close the game, then try again" : strerror(errno);
  }
  return NULL;
}

static void *pack_thread(void *arg) {
  (void)arg;
  char game[PATH_MAX], parent[PATH_MAX], work[PATH_MAX], state[PATH_MAX], backup[PATH_MAX], why[256] = "";
  pthread_mutex_lock(&pack_lock);
  snprintf(game, sizeof game, "%s", pk.game);
  snprintf(backup, sizeof backup, "%s", pk.backup);
  pthread_mutex_unlock(&pack_lock);
  const char *name = strrchr(game, '/') + 1;
  snprintf(parent, sizeof parent, "%.*s", (int)(name - 1 - game), game);
  snprintf(work, sizeof work, "%s/.%s.lz4.partial", parent, name);
  snprintf(state, sizeof state, "%s/.e2a-state", work);

  e2a_list l = {0};
  e2a_game g = {0};
  e2a_pack_stats st = {0};
  e2a_verify_stats vs = {0};
  e2a_pack_opts o = {0};
  uint64_t verified[16] = {0};
  struct statvfs fs;
  const char *err = NULL;
  pack_set("checking", 0);
  e2a_mkdir_p(E2A_STATE_DIR);  /* its log */
  e2a_rm_rf(work);             /* left by a run cut short */
  int ampr = e2a_ampr_compat(game, why, sizeof why);
  if (e2a_read_game(game, &g) || !g.title_id[0]) err = "the game's sce_sys/param.json is unreadable";
  else if (ampr == 0) err = "this game doesn't use AMPR, so LZ4 can't be used for it";
  else if (ampr < 0) err = "its eboot.bin is encrypted, so whether it uses AMPR can't be told";
  else if (e2a_scan_tree(game, &l, true)) err = "couldn't list the game's files";
  else if (!statvfs(parent, &fs) && (unsigned long long)fs.f_bavail * fs.f_frsize < l.total_bytes / 3)
    err = "not enough free space on that drive: LZ4 needs room for about half the game";
  else if (e2a_mkdir_p(state)) err = "can't create the packed copy";
  if (!err) {
    pthread_mutex_lock(&pack_lock);
    pk.before = l.total_bytes;
    pthread_mutex_unlock(&pack_lock);
    e2a_blind_tune(&l);
    err = pack_runtime(work, &l);
  }
  if (!err) {
    char note[300] = "", index[PATH_MAX];
    e2a_list_sort(&l);
    snprintf(index, sizeof index, "%s/ampr_emu.index", work);
    if (e2a_plan_budget(&l, E2A_BLOCK_BUDGET, note, sizeof note) > E2A_BLOCK_LIMIT)
      err = "the game has too many blocks for the AMPR emulator (over 16 million)";
    else if (e2a_write_index(index, &l)) err = "couldn't write ampr_emu.index";
  }
  if (!err) {
    unsigned long long packed = 0, loose = 0;
    o.src_root = game;
    o.out_dir = work;
    o.state_dir = state;
    o.level = PACK_LEVEL;
    o.workers = PACK_WORKERS;
    o.dedup = true;
    o.max_pack_size = E2A_MAX_PACK;
    o.cancel = &pack_cancel;
    o.progress = pack_progress;
    o.verified_bits = verified;
    for (size_t i = 0; i < l.n; i++)
      if (!l.v[i].synthetic) *(l.v[i].action == E2A_LOOSE ? &loose : &packed) += l.v[i].size;
    pack_set("packing", packed);
    if (e2a_pack(&l, &o, &st) != E2A_PACK_OK) err = pack_cancel ? "canceled" : "packing failed";
    if (!err) {
      pack_set("copying", loose);
      if (e2a_copy_loose(&l, &o) != E2A_PACK_OK) err = pack_cancel ? "canceled" : "copying the other files failed";
    }
    if (!err) {
      pack_set("verifying", packed + loose);
      if (e2a_verify(work, &o, &vs) != E2A_PACK_OK) err = pack_cancel ? "canceled" : vs.first_error[0] ? vs.first_error : "the check failed";
    }
  }
  if (!err) {
    e2a_rm_rf(state);
    pack_set("swapping", 0);
    unsigned long long after = dir_bytes(work);
    err = pack_swap(game, work, backup, g.title_id);
    pthread_mutex_lock(&pack_lock);
    pk.after = after;
    pthread_mutex_unlock(&pack_lock);
  }
  if (err) e2a_rm_rf(work);
  e2a_list_free(&l);
  pthread_mutex_lock(&pack_lock);
  snprintf(pk.phase, sizeof pk.phase, "%s", !err ? "done" : !strcmp(err, "canceled") ? "canceled" : "failed");
  snprintf(pk.error, sizeof pk.error, "%s", err && strcmp(err, "canceled") ? err : "");
  pk.running = 0;
  pthread_mutex_unlock(&pack_lock);
  return NULL;
}

/* Claims the job for `kind` on `game` (its set-aside original: `backup`) and starts `run` on it. Replies 409 when a
 * copy or another job is running. */
static void pack_claim(int c, const char *kind, const char *phase, const char *game, const char *backup,
                       void *(*run)(void *)) {
  pthread_mutex_lock(&pack_lock);
  pthread_mutex_lock(&copy_lock);
  int busy = pk.running || cp.running;
  pthread_mutex_unlock(&copy_lock);
  if (!busy) {
    memset(&pk, 0, sizeof pk);
    pk.running = 1;
    pack_cancel = 0;
    snprintf(pk.kind, sizeof pk.kind, "%s", kind);
    snprintf(pk.phase, sizeof pk.phase, "%s", phase);
    snprintf(pk.game, sizeof pk.game, "%s", game);
    snprintf(pk.backup, sizeof pk.backup, "%s", backup);
  }
  pthread_mutex_unlock(&pack_lock);
  if (busy) {
    fail(c, "409 Conflict", "a copy or another packing is running");
    return;
  }
  pthread_t t;
  if (pthread_create(&t, NULL, run, NULL)) {
    pthread_mutex_lock(&pack_lock);
    pk.running = 0;
    pthread_mutex_unlock(&pack_lock);
    fail(c, "500 Internal Server Error", "can't start it");
    return;
  }
  pthread_detach(t);
  ok(c);
}

/* POST /pack?p=<a game folder in a homebrew folder>: starts packing it; GET /pack follows it. */
static void pack_start(int c, const char *path) {
  char backup[PATH_MAX], work[PATH_MAX];
  const char *name = strrchr(path, '/');
  if (!name || !name[1] || name[1] == '.' || !is_dir(path)) {
    fail(c, "400 Bad Request", "p must be a game folder");
    return;
  }
  snprintf(backup, sizeof backup, "%.*s/.%s.lz4-original", (int)(name - path), path, name + 1);
  snprintf(work, sizeof work, "%.*s/.%s.lz4.partial", (int)(name - path), path, name + 1);
  struct stat st;
  if (!lstat(backup, &st)) {
    fail(c, "409 Conflict", "the original of an earlier LZ4 packing is still set aside: delete it or put it back first");
    return;
  }
  pack_claim(c, "pack", "checking", path, backup, pack_thread);
}

/* Deletes the set-aside original (kind delete), or puts it back: the packed copy hidden as .<name>.lz4.partial, its
 * tile removed, the original renamed into place (ShadowMountPlus adds it like a new install), the packed copy deleted.
 * In the background, like packing: a game is tens of gigabytes. */
static void *finish_thread(void *arg) {
  (void)arg;
  char game[PATH_MAX], backup[PATH_MAX], hidden[PATH_MAX], kind[8];
  pthread_mutex_lock(&pack_lock);
  snprintf(game, sizeof game, "%s", pk.game);
  snprintf(backup, sizeof backup, "%s", pk.backup);
  snprintf(kind, sizeof kind, "%s", pk.kind);
  pthread_mutex_unlock(&pack_lock);
  const char *name = strrchr(game, '/') + 1, *err = NULL;
  snprintf(hidden, sizeof hidden, "%.*s/.%s.lz4.partial", (int)(name - 1 - game), game, name);
  e2a_game g = {0};
  if (!strcmp(kind, "delete")) {
    if (e2a_rm_rf(backup)) err = "couldn't delete all of the original";
  } else if (e2a_read_game(backup, &g) || !g.title_id[0]) {
    err = "the original's sce_sys/param.json is unreadable";
  } else {
    e2a_rm_rf(hidden);  /* left by a run cut short */
    if (rename(game, hidden)) {
      err = strerror(errno);
    } else if (remove_tile_waiting(g.title_id) == TILE_REFUSED) {
      rename(hidden, game);
      err = "the game's tile couldn't be removed: close the game, then try again";
    } else if (rename(backup, game)) {
      err = strerror(errno);
      rename(hidden, game);
    } else {
      pack_set("restoring", 0);
      e2a_rm_rf(hidden);
    }
  }
  pthread_mutex_lock(&pack_lock);
  snprintf(pk.phase, sizeof pk.phase, "%s", err ? "failed" : !strcmp(kind, "delete") ? "deleted" : "restored");
  snprintf(pk.error, sizeof pk.error, "%s", err ? err : "");
  pk.running = 0;
  pthread_mutex_unlock(&pack_lock);
  return NULL;
}

/* POST /pack/finish?p=<the packed game>&keep=1 deletes its set-aside original; keep=0 puts the original back. */
static void pack_finish(int c, const char *path, const char *query) {
  char keep[4] = "", backup[PATH_MAX];
  const char *name = strrchr(path, '/');
  struct stat st;
  if (!name || !name[1] || name[1] == '.' || !param(query, "keep", keep, sizeof keep)) {
    fail(c, "400 Bad Request", "p must be a game folder, and keep 1 or 0");
    return;
  }
  snprintf(backup, sizeof backup, "%.*s/.%s.lz4-original", (int)(name - path), path, name + 1);
  if (lstat(backup, &st)) {
    fail(c, "404 Not Found", "no original is set aside for this game");
    return;
  }
  int del = keep[0] == '1';
  pack_claim(c, del ? "delete" : "restore", del ? "deleting" : "restoring", path, backup, finish_thread);
}

/* The title and version in a param.json (the first titleName: the default language's) into `g`; left empty when the
 * file is missing or unreadable. */
static void read_param(const char *file, e2a_game *g) {
  char *doc = NULL;
  if (e2a_read_text(file, &doc, NULL, 1 << 20)) return;
  json_str(doc, "titleName", g->title, sizeof g->title);
  json_str(doc, "contentVersion", g->version, sizeof g->version);
  free(doc);
}

static void list_game(buf_t *b, const char *path, const char *kind, e2a_game *g, const char *fallback, int packed, int original) {
  if (!g->title[0]) snprintf(g->title, sizeof g->title, "%s", fallback);
  for (char *t = g->title; *t; t++)  /* the line format's separators */
    if (*t == '\t' || *t == '\n' || *t == '\r') *t = ' ';
  for (char *t = g->version; *t; t++)
    if (*t == '\t' || *t == '\n' || *t == '\r') *t = ' ';
  put(b, "%s\t%s\t%s\t%d\t%d\t%s\t%s\n", path, g->title_id, g->title, packed, original, kind, g->version);
}

/* GET /local/games, for the PS5 app (its Installed tab with the PC away, and its LZ4 tab): what's installed on this
 * console, one per line, tab-separated: path, title ID, title, packed (1/0), original set aside (1/0), kind (folder,
 * image or package), version. As the PC app lists it (FTPConsoleAdapter._games): games installed from a package in
 * <user>/app, named by the console's copy of their param.json in <user>/appmeta, and ShadowMountPlus's game folders
 * and disc images in the homebrew folders. Plain text: the app has no JSON parser. */
static void local_games(int c) {
  static const char *const images[] = {".exfat", ".ffpkg", ".ffpfs", ".ffpfsc"};
  buf_t b = {0};
  char dir[32], path[PATH_MAX], probe[PATH_MAX];
  struct stat st;
  struct dirent *e;
  for (int d = 0; d < 9; d++) {  /* the internal SSD's user folder, then the M.2 drives' */
    if (d == 0) snprintf(dir, sizeof dir, "/user");
    else snprintf(dir, sizeof dir, "/mnt/ext%d/user", d - 1);
    snprintf(path, sizeof path, "%s/app", dir);
    DIR *apps = opendir(path);
    while (apps && (e = readdir(apps))) {
      e2a_game g = {0};
      if (strlen(e->d_name) != 9 || !title_id_at(e->d_name) || !strncmp(e->d_name, "NPXS", 4)) continue;  /* NPXS: system */
      snprintf(probe, sizeof probe, "%s/app/%s/app.pkg", dir, e->d_name);
      if (stat(probe, &st)) continue;  /* ShadowMountPlus's stub (listed below) or a launcher */
      snprintf(g.title_id, sizeof g.title_id, "%s", e->d_name);
      snprintf(probe, sizeof probe, "%s/appmeta/%s/param.json", dir, e->d_name);  /* a PS4 game's is param.sfo: its ID shows */
      read_param(probe, &g);
      snprintf(path, sizeof path, "%s/app/%s", dir, e->d_name);
      list_game(&b, path, "package", &g, e->d_name, 0, 0);
    }
    if (apps) closedir(apps);
  }
  for (int d = 0; d < 17; d++) {
    if (d == 0) snprintf(dir, sizeof dir, "/data/homebrew");
    else snprintf(dir, sizeof dir, "/mnt/%s%d/homebrew", d <= 8 ? "usb" : "ext", (d - 1) % 8);
    DIR *hb = opendir(dir);
    while (hb && (e = readdir(hb))) {
      e2a_game g = {0};
      const char *name = e->d_name, *dot = strrchr(name, '.');
      if (name[0] == '.' || snprintf(path, sizeof path, "%s/%s", dir, name) >= (int)sizeof path) continue;
      if (is_dir(path)) {
        if (e2a_read_game(path, &g) || !g.title_id[0] || !strcmp(g.title_id, "PPSA99787")) continue;  /* not a game, or the PS5 app */
        snprintf(probe, sizeof probe, "%s/ampr_assets.index", path);
        int packed = !stat(probe, &st);
        snprintf(probe, sizeof probe, "%s/.%s.lz4-original", dir, name);
        list_game(&b, path, "folder", &g, name, packed, !lstat(probe, &st));
        continue;
      }
      int image = 0;
      for (int i = 0; dot && i < 4; i++) image |= !strcasecmp(dot, images[i]);
      for (const char *t = name; image && strlen(t) >= 9 && !g.title_id[0]; t++)  /* its title ID is in its name */
        if (title_id_at(t)) snprintf(g.title_id, sizeof g.title_id, "%.9s", t);
      if (!g.title_id[0]) continue;
      snprintf(probe, sizeof probe, "/user/appmeta/%s/param.json", g.title_id);  /* registered: the console's copy */
      read_param(probe, &g);
      list_game(&b, path, "image", &g, name, 0, 0);
    }
    if (hb) closedir(hb);
  }
  reply(c, "200 OK", &b);
  free(b.s);
}

static void pack_status(int c) {
  buf_t b = {0};
  pthread_mutex_lock(&pack_lock);
  put(&b, "{\"running\":%s,\"kind\":", pk.running ? "true" : "false");
  put_str(&b, pk.kind);
  put(&b, ",\"phase\":");
  put_str(&b, pk.phase);
  put(&b, ",\"done\":%llu,\"total\":%llu,\"before\":%llu,\"after\":%llu,\"game\":", pk.done, pk.total, pk.before, pk.after);
  put_str(&b, pk.game);
  put(&b, ",\"backup\":");
  put_str(&b, pk.backup);
  put(&b, ",\"error\":");
  put_str(&b, pk.error);
  pthread_mutex_unlock(&pack_lock);
  put(&b, "}");
  reply(c, "200 OK", &b);
  free(b.s);
}

/* GET /local/pack, for the PS5 app: GET /pack as one tab-separated line: running (1/0), kind, phase, done, total,
 * before, after, game, error. */
static void local_pack(int c) {
  buf_t b = {0};
  pthread_mutex_lock(&pack_lock);
  put(&b, "%d\t%s\t%s\t%llu\t%llu\t%llu\t%llu\t%s\t%s\n", pk.running, pk.kind, pk.phase, pk.done, pk.total, pk.before,
      pk.after, pk.game, pk.error);
  pthread_mutex_unlock(&pack_lock);
  reply(c, "200 OK", &b);
  free(b.s);
}

#define LOGO_KEEP "/data/spela/logos" /* <title ID>.png: the last logo set for each title, for keep_logos() */
static pthread_mutex_t logo_lock = PTHREAD_MUTEX_INITIALIZER;

/* Copies file `src` over `dst` through <dst>.partial, so a copy cut short never replaces it. NULL when done, else what
 * went wrong. */
static const char *copy_file(const char *src, const char *dst) {
  char tmp[PATH_MAX], chunk[16384];
  if (snprintf(tmp, sizeof tmp, "%s.partial", dst) >= (int)sizeof tmp) return "path too long";
  int in = open(src, O_RDONLY), out = in < 0 ? -1 : open(tmp, O_WRONLY | O_CREAT | O_TRUNC, 0666);
  const char *err = in < 0 || out < 0 ? strerror(errno) : NULL;
  ssize_t r;
  while (!err && (r = read(in, chunk, sizeof chunk)) != 0) {
    errno = 0;
    if (r < 0 || write(out, chunk, r) != r) err = errno ? strerror(errno) : "write cut short";
  }
  if (in >= 0) close(in);
  if (out >= 0 && close(out) && !err) err = strerror(errno);
  if (!err && rename(tmp, dst)) err = strerror(errno);
  if (err) unlink(tmp);
  return err;
}

/* Points `id`'s logoImageInfo in the home screen's database at `info`, or back to NULL. The rows changed (0: the home
 * screen has no entry for it), or -1 with the reason in `why`. SQLite edits the database in place, honouring the home
 * screen's locks. */
static int point_logo(const char *id, const char *info, char *why, size_t n) {
  sqlite3 *db = NULL;
  sqlite3_stmt *st = NULL;
  int changed = -1;
  if (sqlite3_open_v2(APP_DB, &db, SQLITE_OPEN_READWRITE, NULL) == SQLITE_OK) {
    sqlite3_busy_timeout(db, 10000);  /* the home screen holds its lock only briefly */
    if (sqlite3_prepare_v2(db, "UPDATE tbl_conceptmetadata SET logoImageInfo = ?1 WHERE localConceptId = "
                               "(SELECT localConceptId FROM tbl_contentinfo WHERE titleId = ?2)", -1, &st, NULL) == SQLITE_OK) {
      if (info) sqlite3_bind_text(st, 1, info, -1, SQLITE_TRANSIENT);
      else sqlite3_bind_null(st, 1);
      sqlite3_bind_text(st, 2, id, -1, SQLITE_TRANSIENT);
      if (sqlite3_step(st) == SQLITE_DONE) changed = sqlite3_changes(db);
    }
    if (changed < 0) snprintf(why, n, "%s", sqlite3_errmsg(db));
  } else {
    snprintf(why, n, "can't open the home screen's database");
  }
  sqlite3_finalize(st);
  sqlite3_close(db);
  return changed;
}

/* `id`'s logoImageInfo into `info`: 1 when it has one, 0 when it's NULL, -1 when the home screen has no entry for it
 * (or the database can't be read). */
static int logo_info(const char *id, char *info, size_t n) {
  sqlite3 *db = NULL;
  sqlite3_stmt *st = NULL;
  int r = -1;
  if (sqlite3_open_v2(APP_DB, &db, SQLITE_OPEN_READONLY, NULL) == SQLITE_OK) {
    sqlite3_busy_timeout(db, 10000);
    if (sqlite3_prepare_v2(db, "SELECT m.logoImageInfo FROM tbl_contentinfo c JOIN tbl_conceptmetadata m "
                               "ON m.localConceptId = c.localConceptId WHERE c.titleId = ?1", -1, &st, NULL) == SQLITE_OK) {
      sqlite3_bind_text(st, 1, id, -1, SQLITE_TRANSIENT);
      if (sqlite3_step(st) == SQLITE_ROW) {
        const unsigned char *v = sqlite3_column_text(st, 0);
        r = v != NULL;
        snprintf(info, n, "%s", v ? (const char *)v : "");
      }
    }
  }
  sqlite3_finalize(st);
  sqlite3_close(db);
  return r;
}

/* Shows a title logo on `id`'s home-screen page instead of its name in plain text: writes the PNG body to
 * /user/appmeta/<id>/logo.png and points the title's logoImageInfo in the home screen's database at it. The home
 * screen can't read files under /data/homebrew, hence appmeta. An empty body undoes that: logoImageInfo back to NULL,
 * as the console leaves it, and the file gone. Only that one file and that one column change, and only for a title
 * the console already has. A copy is kept in LOGO_KEEP, so the logo comes back after a restart without the PC. */
static void set_logo(int c, const char *query, const char *head, const char *body, size_t have) {
  static const char not_png[] = "not a PNG";
  char id[16], dir[64], tmp[96], dest[96], info[128], keep[64], why[256];
  unsigned char sig[8] = {0};
  long long len = content_length(head);
  if (!title_id(query, id)) {
    fail(c, "400 Bad Request", "id must be a title ID");
    return;
  }
  if (len && (len < 8 || len > LOGO_MAX)) {  /* an older helper answers an empty body here: the PC tells it by that */
    fail(c, "413 Content Too Large", "the logo must be a PNG of at most 16 MB");
    return;
  }
  snprintf(dir, sizeof dir, "/user/appmeta/%s", id);
  if (!is_dir(dir)) {
    fail(c, "404 Not Found", "this game isn't on the console");
    return;
  }
  snprintf(tmp, sizeof tmp, "%s/.logo.png.partial", dir);
  snprintf(dest, sizeof dest, "%s/logo.png", dir);
  snprintf(info, sizeof info, "%s?ts=%lld", dest, (long long)time(NULL));  /* ts: the home screen reloads the image */
  snprintf(keep, sizeof keep, LOGO_KEEP "/%s.png", id);

  pthread_mutex_lock(&logo_lock);
  const char *err = NULL;
  if (len) {
    err = receive(c, tmp, len, body, have);
    int fd = err ? -1 : open(tmp, O_RDONLY);
    if (!err && (fd < 0 || read(fd, sig, 8) != 8 || memcmp(sig, "\x89PNG\r\n\x1a\n", 8))) err = not_png;
    if (fd >= 0) close(fd);
    if (!err && rename(tmp, dest)) err = strerror(errno);
    if (err) unlink(tmp);
  }
  int changed = 0;
  if (!err && (changed = point_logo(id, len ? info : NULL, why, sizeof why)) < 0) err = why;
  if (err) fail(c, err == not_png ? "400 Bad Request" : "500 Internal Server Error", err);
  else if (!changed) fail(c, "404 Not Found", "the home screen has no entry for this game");
  else {
    if (!len) {
      unlink(dest);  /* already gone is fine */
      unlink(keep);
    } else {
      mkdir("/data/spela", 0777);
      mkdir(LOGO_KEEP, 0777);
      copy_file(dest, keep);  /* ponytail: a copy that fails only means no logo after the next restart */
    }
    ok(c);
  }
  pthread_mutex_unlock(&logo_lock);
}

/* A restart of the PS5 registers its games again, which drops their logos. Every minute (ShadowMountPlus adds games
 * back a few minutes after a restart, or any time) this puts back each logo kept in LOGO_KEEP whose title is on the home
 * screen without one, so they come back with no PC around. A title showing another logo (its own from PlayStation
 * Network) is left alone. */
static void *keep_logos(void *arg) {
  (void)arg;
  for (;; sleep(60)) {
    DIR *d = opendir(LOGO_KEEP);
    struct dirent *e;
    while (d && (e = readdir(d))) {
      if (strlen(e->d_name) != 13 || !title_id_at(e->d_name) || strcmp(e->d_name + 9, ".png")) continue;
      char id[16], keep[64], dest[96], info[256], why[256];
      struct stat st;
      snprintf(id, sizeof id, "%.9s", e->d_name);
      snprintf(keep, sizeof keep, LOGO_KEEP "/%s", e->d_name);
      snprintf(dest, sizeof dest, "/user/appmeta/%s/logo.png", id);
      pthread_mutex_lock(&logo_lock);
      int has = logo_info(id, info, sizeof info);
      if (has == 0 || (has == 1 && !strncmp(info, dest, strlen(dest)) && stat(dest, &st))) {
        snprintf(info, sizeof info, "%s?ts=%lld", dest, (long long)time(NULL));
        if (!copy_file(keep, dest)) point_logo(id, info, why, sizeof why);  /* what fails is tried next minute */
      }
      pthread_mutex_unlock(&logo_lock);
    }
    if (d) closedir(d);
  }
  return NULL;
}

/* The value of header `name` (any case) in `head`, which ends at the blank line; NULL without one. */
static const char *header(const char *head, const char *name) {
  size_t n = strlen(name);
  for (const char *l = strstr(head, "\r\n"); l; l = strstr(l + 2, "\r\n"))
    if (!strncasecmp(l + 2, name, n) && l[2 + n] == ':') {
      for (l += 3 + n; *l == ' ' || *l == '\t'; l++);
      return l;
    }
  return NULL;
}

/* An IPv4 address, optionally with a port, or localhost: how the PC addresses us, never a web site's name. */
static int ip_host(const char *h) {
  size_t n = strcspn(h, "\r\n:");
  if (n == 9 && !strncasecmp(h, "localhost", 9)) return 1;
  if (!n || strspn(h, "0123456789.") != n) return 0;
  return h[n] != ':' || strspn(h + n + 1, "0123456789") == strcspn(h + n + 1, "\r\n");
}

/* ---- The PS5 app's way to the PC ------------------------------------------------------------------------------
 * The PS5 app on this console asks us (127.0.0.1) for /api/v1/console-app/..., the PC's API for it. We hold each
 * request in a slot until the PC, which keeps asking GET /relay, takes it, runs it on itself and sends the answer
 * with POST /relay. The PC only ever connects out to the console, so its firewall never gets in the way. */
#define RELAY_SLOTS 8
#define RELAY_PATH "/api/v1/console-app/"
#define RELAY_BODY_MAX (16 << 20)
static struct {
  int used, taken, done, status;
  unsigned id;
  char method[8], path[1024], auth[192];
  char *body;
  size_t len;
} slots[RELAY_SLOTS];
static unsigned relay_next = 1;
static pthread_mutex_t relay_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t relay_cond = PTHREAD_COND_INITIALIZER;  /* broadcast on every change; waiters recheck */

/* Waits for relay_cond until `deadline`; 0 once it has passed. Called with relay_lock held. */
static int relay_wait(const struct timespec *deadline) {
  return pthread_cond_timedwait(&relay_cond, &relay_lock, deadline) != ETIMEDOUT;
}

static struct timespec relay_in(int ms) {
  struct timespec t;
  clock_gettime(CLOCK_REALTIME, &t);
  t.tv_sec += ms / 1000;
  t.tv_nsec += (long)(ms % 1000) * 1000000;
  if (t.tv_nsec >= 1000000000) t.tv_sec++, t.tv_nsec -= 1000000000;
  return t;
}

/* Sends `body` as a plain-text answer: the PS5 app reads the status line and everything after the headers. */
static void relay_reply(int c, int status, const char *body, size_t len) {
  char head[160];
  int n = snprintf(head, sizeof head, "HTTP/1.0 %d Relay\r\nContent-Type: text/plain; charset=utf-8\r\n"
                   "Content-Length: %zu\r\nConnection: close\r\n\r\n", status, len);
  if (!send_all(c, head, n)) send_all(c, body, len);
}

/* A request from the PS5 app: queued for the PC, then answered with what the PC sent. The app waits 5 seconds for an
 * answer, so a PC that doesn't take it within 3 is told about in words instead. */
static void relay_app(int c, const char *method, const char *path, const char *head) {
  static const char no_pc[] = "Spela on your PC isn't answering. Check that it's running and that this PS5 is added "
                              "under Settings, Consoles.";
  static const char busy[] = "Too many requests at once. Try again.";
  const char *auth = header(head, "Authorization");
  pthread_mutex_lock(&relay_lock);
  int i = 0;
  while (i < RELAY_SLOTS && slots[i].used) i++;
  if (i == RELAY_SLOTS) {
    pthread_mutex_unlock(&relay_lock);
    relay_reply(c, 503, busy, sizeof busy - 1);
    return;
  }
  unsigned id = relay_next++;
  memset(&slots[i], 0, sizeof slots[i]);
  slots[i].used = 1;
  slots[i].id = id;
  snprintf(slots[i].method, sizeof slots[i].method, "%s", method);
  snprintf(slots[i].path, sizeof slots[i].path, "%s", path);
  if (auth) snprintf(slots[i].auth, sizeof slots[i].auth, "%.*s", (int)strcspn(auth, "\r\n"), auth);
  pthread_cond_broadcast(&relay_cond);
  struct timespec pickup = relay_in(3000), answer = relay_in(4500);
  while (!slots[i].done && relay_wait(slots[i].taken ? &answer : &pickup));
  int status = slots[i].done ? slots[i].status : 0;
  char *body = slots[i].body;
  size_t len = slots[i].len;
  slots[i].used = 0;  /* a late answer from the PC finds no slot and is dropped */
  slots[i].body = NULL;
  pthread_mutex_unlock(&relay_lock);
  if (status) relay_reply(c, status, body, len);
  else relay_reply(c, 503, no_pc, sizeof no_pc - 1);
  free(body);
}

/* GET /relay (the PC): the oldest request nobody has taken, as JSON, waiting up to 20 seconds for one; 204 if none. */
static void relay_take(int c) {
  struct timespec until = relay_in(20000);
  pthread_mutex_lock(&relay_lock);
  for (;;) {
    int pick = -1;
    for (int i = 0; i < RELAY_SLOTS; i++)
      if (slots[i].used && !slots[i].taken && (pick < 0 || slots[i].id < slots[pick].id)) pick = i;
    if (pick >= 0) {
      slots[pick].taken = 1;
      pthread_cond_broadcast(&relay_cond);
      buf_t b = {0};
      put(&b, "{\"id\":%u,\"method\":", slots[pick].id);
      put_str(&b, slots[pick].method);
      put(&b, ",\"path\":");
      put_str(&b, slots[pick].path);
      put(&b, ",\"auth\":");
      put_str(&b, slots[pick].auth);
      put(&b, "}");
      pthread_mutex_unlock(&relay_lock);
      reply(c, "200 OK", &b);
      free(b.s);
      return;
    }
    if (!relay_wait(&until)) break;
  }
  pthread_mutex_unlock(&relay_lock);
  buf_t empty = {0};
  reply(c, "204 No Content", &empty);
}

/* POST /relay?id=<id>&status=<HTTP status> (the PC): the answer to a request it took, as the body. */
static void relay_answer(int c, const char *query, const char *head, const char *body, size_t have) {
  char id_s[16] = "", status_s[8] = "";
  long long len = content_length(head);
  if (!query || !param(query, "id", id_s, sizeof id_s) || !param(query, "status", status_s, sizeof status_s)) {
    fail(c, "400 Bad Request", "id and status are needed");
    return;
  }
  if (len < 0 || len > RELAY_BODY_MAX) {
    fail(c, "413 Content Too Large", "the answer needs a Content-Length of at most 16 MB");
    return;
  }
  char *data = malloc(len ? len : 1);
  size_t got = have > (size_t)len ? (size_t)len : have;
  if (data) memcpy(data, body, got);
  while (data && got < (size_t)len) {
    ssize_t r = recv(c, data + got, len - got, 0);
    if (r <= 0) {
      free(data);
      data = NULL;
    } else {
      got += r;
    }
  }
  if (!data) {
    fail(c, "400 Bad Request", "the answer was cut off");
    return;
  }
  unsigned id = (unsigned)strtoul(id_s, NULL, 10);
  pthread_mutex_lock(&relay_lock);
  int i = 0;
  while (i < RELAY_SLOTS && !(slots[i].used && slots[i].taken && !slots[i].done && slots[i].id == id)) i++;
  if (i < RELAY_SLOTS) {
    slots[i].status = atoi(status_s);
    slots[i].body = data;
    slots[i].len = len;
    slots[i].done = 1;
    pthread_cond_broadcast(&relay_cond);
  }
  pthread_mutex_unlock(&relay_lock);
  if (i < RELAY_SLOTS) {
    ok(c);
  } else {
    free(data);
    fail(c, "404 Not Found", "the PS5 app stopped waiting");
  }
}

/* Whether the request came from this console itself. */
static int from_loopback(int c) {
  struct sockaddr_in peer;
  socklen_t len = sizeof peer;
  return !getpeername(c, (struct sockaddr *)&peer, &len) && peer.sin_family == AF_INET &&
         peer.sin_addr.s_addr == htonl(INADDR_LOOPBACK);
}

/* Whether the request may run. The PC app sends this console's key. Web pages are refused outright: a browser marks
 * its requests with Origin or Sec-Fetch-Site, and DNS rebinding shows as a name in Host. Only this console itself may
 * ask without the key (`local`): for /quit (stop_old_copy(), which may hold an older key) and the PS5 app's requests,
 * which carry the PC's pairing key for the PC to check. */
static int authorized(int c, const char *head, int local) {
  const char *host = header(head, "Host"), *key = header(head, "X-Storefront-Key");
  if (header(head, "Origin") || header(head, "Sec-Fetch-Site") || (host && !ip_host(host))) return 0;
  if (local && from_loopback(c)) return 1;
  const volatile char *want = KEY_SLOT + sizeof KEY_PREFIX - 1;
  if (!key || want[0] == '?' || strcspn(key, "\r\n \t") != KEY_LEN) return 0;  /* unpatched: the placeholder is public */
  unsigned char diff = 0;
  for (int i = 0; i < KEY_LEN; i++) diff |= key[i] ^ want[i];  /* the same time whichever byte differs */
  return !diff;
}

/* Answers one request on connection `c`. */
static void handle(int c) {
  char head[8192], method[8] = "", target[4096] = "", path[PATH_MAX], to[PATH_MAX], *end = NULL;
  size_t got = 0;
  ssize_t r;
  while (got < sizeof head - 1 && (r = recv(c, head + got, sizeof head - 1 - got, 0)) > 0) {
    got += r;
    head[got] = 0;
    if ((end = strstr(head, "\r\n\r\n"))) break;
  }
  if (!end) return;
  *end = 0;
  sscanf(head, "%7s %4095s", method, target);
  char *query = strchr(target, '?');
  if (query) *query++ = 0;
  int get = !strcmp(method, "GET"), post = !strcmp(method, "POST"), has_path = query && param(query, "p", path, sizeof path) && path[0] == '/';

  /* save data takes plain file writes only (save_path()), never /copy or /install */
  int put_file = !strcmp(method, "PUT") && !strcmp(target, "/file"), save_op = 0;
  int post_file = post && (!strcmp(target, "/mkdir") || !strcmp(target, "/rename") || !strcmp(target, "/rm"));
  int app = !strncmp(target, RELAY_PATH, sizeof RELAY_PATH - 1);  /* the PS5 app, for the PC */
  int local = !strncmp(target, "/local/", 7);  /* the PS5 app, for us: LZ4 with the PC away */
  if (!authorized(c, head, (post && !strcmp(target, "/quit")) || app || local)) {
    buf_t b = {0};
    put(&b, "{\"error\":\"wrong key: install the helper again from the PC app\",\"owner\":");
    put_str(&b, owner);
    put(&b, "}");
    reply(c, "401 Unauthorized", &b);
    free(b.s);
  } else if (app) {
    if (!from_loopback(c)) {  /* the PC's own API is on the PC: nobody else reaches it through us */
      fail(c, "403 Forbidden", "only the PS5 app on this console can ask for this");
    } else {
      char full[1024];
      snprintf(full, sizeof full, "%s%s%s", target, query ? "?" : "", query ? query : "");
      relay_app(c, method, full, head);
    }
  } else if (local && !from_loopback(c)) {
    fail(c, "403 Forbidden", "only the PS5 app on this console can ask for this");
  } else if (local && get && !strcmp(target, "/local/games")) {
    local_games(c);
  } else if (local && get && !strcmp(target, "/local/pack")) {
    local_pack(c);
  } else if (local && post && !strcmp(target, "/local/pack/cancel")) {
    pack_cancel = 1;
    ok(c);
  } else if (local && post && (!has_path || !writable(path))) {
    fail(c, "403 Forbidden", "p must be a game folder in a homebrew folder");
  } else if (local && post && !strcmp(target, "/local/pack")) {
    pack_start(c, path);
  } else if (local && post && !strcmp(target, "/local/pack/finish")) {
    pack_finish(c, path, query);
  } else if (local) {
    fail(c, "404 Not Found", "not_found");
  } else if (get && !strcmp(target, "/relay")) {
    relay_take(c);
  } else if (post && !strcmp(target, "/relay")) {
    relay_answer(c, query, head, end + 4, got - (end + 4 - head));
  } else if (get && !strcmp(target, "/info")) {
    buf_t b = {0};
    info(&b);
    reply(c, "200 OK", &b);
    free(b.s);
  } else if (post && !strcmp(target, "/quit")) {
    ok(c);
    close(c);
    exit(0);
  } else if (post && !strcmp(target, "/uninstall")) {
    uninstall_tile(c, query);
  } else if (post && !strcmp(target, "/logo")) {
    set_logo(c, query, head, end + 4, got - (end + 4 - head));
  } else if (get && !strcmp(target, "/klog")) {
    char s[8] = "30";
    if (query) param(query, "s", s, sizeof s);
    int n = atoi(s);
    klog(c, n < 1 ? 1 : n > 120 ? 120 : n);
  } else if (get && !strcmp(target, "/copy")) {
    copy_status(c);
  } else if (post && !strcmp(target, "/copy/cancel")) {
    cp.cancel = 1;
    ok(c);
  } else if (get && !strcmp(target, "/pack")) {
    pack_status(c);
  } else if (post && !strcmp(target, "/pack/cancel")) {
    pack_cancel = 1;
    ok(c);
  } else if (!has_path && (get || post || !strcmp(method, "PUT"))) {
    fail(c, "400 Bad Request", "p must be an absolute path");
  } else if (get && !strcmp(target, "/ls")) {
    list(c, path);
  } else if (get && !strcmp(target, "/file")) {
    download(c, path);
  } else if (!writable(path) && !(save_op = save_path(path) && (put_file || post_file))) {
    fail(c, "403 Forbidden", "only the homebrew folders and save data can be changed");
  } else if (post && !strcmp(target, "/copy")) {
    copy_start(c, path, query);
  } else if (post && !strcmp(target, "/pack")) {
    pack_start(c, path);
  } else if (post && !strcmp(target, "/pack/finish")) {
    pack_finish(c, path, query);
  } else if (post && !strcmp(target, "/install")) {
    install_pkg(c, path, query);
  } else if (!strcmp(method, "PUT") && !strcmp(target, "/file")) {
    upload(c, path, head, end + 4, got - (end + 4 - head));
  } else if (post && !strcmp(target, "/mkdir")) {
    if (mkdir(path, 0777)) fail(c, errno == EEXIST ? "409 Conflict" : "500 Internal Server Error", strerror(errno));
    else ok(c);
  } else if (post && !strcmp(target, "/rename")) {
    if (!param(query, "to", to, sizeof to) || !(save_op ? save_path(to) : writable(to)))
      fail(c, "403 Forbidden", "only the homebrew folders and save data can be changed");
    else if (rename(path, to)) fail(c, "500 Internal Server Error", strerror(errno));
    else ok(c);
  } else if (post && !strcmp(target, "/rm")) {
    if (is_dir(path) ? rmdir(path) : unlink(path)) fail(c, "500 Internal Server Error", strerror(errno));
    else ok(c);
  } else {
    fail(c, "404 Not Found", "not_found");
  }
}

static void *serve(void *arg) {
  int c = (int)(intptr_t)arg;
  handle(c);
  close(c);
  return NULL;
}

static void notify(const char *msg) {
  notify_request_t req;
  memset(&req, 0, sizeof req);
  strncpy(req.message, msg, sizeof req.message - 1);
  sceKernelSendNotificationRequest(0, &req, sizeof req, 0);
}

/* Asks a helper that's already running (an older copy) to stop, so this one can take the port. */
static void stop_old_copy(void) {
  int s = socket(AF_INET, SOCK_STREAM, 0);
  struct sockaddr_in addr;
  memset(&addr, 0, sizeof addr);
  addr.sin_len = sizeof addr;
  addr.sin_family = AF_INET;
  addr.sin_port = htons(PORT);
  addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  if (!connect(s, (struct sockaddr *)&addr, sizeof addr)) {
    const char *req = "POST /quit HTTP/1.0\r\n\r\n";
    send(s, req, strlen(req), 0);
    char buf[256];
    while (recv(s, buf, sizeof buf, 0) > 0);
  }
  close(s);
}

int main(void) {
  /* the kernel keeps 19 characters; Payload Manager lists processes whose name ends in .elf */
  syscall(SYS_thr_set_name, -1, "spela.elf");
  signal(SIGPIPE, SIG_IGN);
  read_owner();
  int srv = socket(AF_INET, SOCK_STREAM, 0), one = 1;
  setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
  struct sockaddr_in addr;
  memset(&addr, 0, sizeof addr);
  addr.sin_len = sizeof addr;
  addr.sin_family = AF_INET;
  addr.sin_port = htons(PORT);
  addr.sin_addr.s_addr = htonl(INADDR_ANY);
  for (int tries = 0; bind(srv, (struct sockaddr *)&addr, sizeof addr); tries++) {
    if (tries == 0) stop_old_copy();
    if (tries == 20) {  /* something that isn't our helper holds the port */
      notify("Spela helper couldn't start: port 7735 is in use.");
      return 1;
    }
    sleep(1);
  }
  listen(srv, 16);
  char msg[128];
  snprintf(msg, sizeof msg, owner[0] ? "Spela helper " VERSION " is running for %s." : "Spela helper " VERSION " is running.", owner);
  notify(msg);
  pthread_t retry;
  if (!pthread_create(&retry, NULL, retry_tiles, NULL)) pthread_detach(retry);
  pthread_t logos;
  if (!pthread_create(&logos, NULL, keep_logos, NULL)) pthread_detach(logos);

  struct timeval timeout = {30, 0};  /* a client that stops sending gives up its thread */
  for (;;) {
    int c = accept(srv, NULL, NULL);
    if (c < 0) continue;
    setsockopt(c, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof timeout);
    pthread_t t;
    if (pthread_create(&t, NULL, serve, (void *)(intptr_t)c)) close(c);
    else pthread_detach(t);
  }
}
