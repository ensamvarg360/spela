/* Spela helper: a small HTTP server the PC app loads through the console's ELF loader.
 *   GET  /info                 {"version", "firmware", "model", "mounts": [{"mount", "total", "free"}]}
 *   GET  /ls?p=<dir>           [{"name", "dir", "size"}]; 404 when the folder is missing
 *   GET  /file?p=<file>        the file's bytes
 *   PUT  /file?p=<file>        writes the request body (needs Content-Length) to the file
 *   POST /mkdir?p=<dir>  /rename?p=<from>&to=<to>  /rm?p=<file or empty folder>
 *   POST /quit                 stops the helper, so a newer copy can take the port (a new copy sends it to an old one itself)
 *   POST /uninstall?id=<title ID>  removes a ShadowMountPlus tile whose game files are gone (uninstall_tile())
 *   POST /logo?id=<title ID>   PNG body: the title logo on the game's home-screen page (set_logo())
 *   POST /copy?p=<from>&to=<to>  starts copying a file or folder on the console, e.g. to another drive (copy_start())
 *   GET  /copy                 {"running", "done", "total", "error", "to"} of that copy;  POST /copy/cancel stops it
 *   POST /install?p=<.pkg in a homebrew folder>&cid=<content ID>  starts the console's package installer (install_pkg(), untested)
 *   GET  /klog?s=<seconds>     the kernel log as it's written, for up to 120 seconds: the loader's reason when an app won't start
 *   GET  /relay  POST /relay?id=<id>&status=<status>  the PS5 app's requests, taken and answered by the PC (relay_app())
 *   any  /api/v1/console-app/...  from this console only, without the key: the PS5 app, passed on to the PC
 * Paths are percent-encoded. Every request needs this console's key in X-Storefront-Key (authorized()); even so,
 * writes are refused outside the homebrew folders (writable()), and /logo changes only the one file and database column it names. One thread per connection: /info still answers during a long upload.
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

#include "sqlite3.h"  /* the amalgamation build.ps1 downloads; compiled in, the console has no SQLite to link */

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

/* Only inside /data/homebrew, /mnt/usbN/homebrew or /mnt/extN/homebrew (or that folder itself), and no "..". */
static int writable(const char *p) {
  const char *rest = NULL;
  if (!strncmp(p, "/data/", 6)) rest = p + 5;
  else if ((!strncmp(p, "/mnt/usb", 8) || !strncmp(p, "/mnt/ext", 8)) && p[8] >= '0' && p[8] <= '9') rest = p + 9;
  if (!rest || strncmp(rest, "/homebrew", 9) || (rest[9] && rest[9] != '/')) return 0;
  for (const char *s = p; (s = strstr(s, "/..")); s += 3)
    if (!s[3] || s[3] == '/') return 0;
  return 1;
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

/* Query parameter "id" as a title ID (4 capital letters, 5 digits) into `id` (16 bytes); 0 when it isn't one. */
static int title_id(const char *query, char *id) {
  if (!query || !param(query, "id", id, 16) || strlen(id) != 9) return 0;
  for (int i = 0; i < 9; i++)
    if (i < 4 ? id[i] < 'A' || id[i] > 'Z' : id[i] < '0' || id[i] > '9') return 0;
  return 1;
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

/* Removes the home-screen tile of a ShadowMountPlus game whose files were deleted. Only such tiles: the title must
 * have ShadowMountPlus's /user/app/<id>/mount.lnk, and the source it names (mount_img.lnk for a disc image) must be
 * gone, so nothing on the network can remove a game that's still there. No tile at all counts as done. */
static void uninstall_tile(int c, const char *query) {
  char id[16] = "", lnk[PATH_MAX], source[PATH_MAX] = "", msg[4096];
  if (!title_id(query, id)) {
    fail(c, "400 Bad Request", "id must be a title ID");
    return;
  }
  FILE *f = NULL;
  snprintf(lnk, sizeof lnk, "/user/app/%s/mount_img.lnk", id);
  if (!(f = fopen(lnk, "r"))) {
    snprintf(lnk, sizeof lnk, "/user/app/%s/mount.lnk", id);
    f = fopen(lnk, "r");
  }
  if (!f) {
    ok(c);  /* not a ShadowMountPlus title, or its tile is already gone */
    return;
  }
  if (!fgets(source, sizeof source, f)) source[0] = 0;
  fclose(f);
  source[strcspn(source, "\r\n")] = 0;
  struct stat st;
  if (!source[0] || !lstat(source, &st)) {
    fail(c, "409 Conflict", "the game's files are still on the console");
    return;
  }
  int status = smp_uninstall(id, msg, sizeof msg);
  if (status == 200) {
    ok(c);
  } else if (status) {
    buf_t b = {0};  /* pass on ShadowMountPlus's answer: {"status", "error", "error_reason"} */
    put(&b, "%s", msg);
    char line[32];
    snprintf(line, sizeof line, "%d ShadowMountPlus", status);
    reply(c, line, &b);
    free(b.s);
  } else if ((status = appinstutil_uninstall(id))) {
    snprintf(msg, sizeof msg, "the console refused (0x%08X)", (unsigned)status);
    fail(c, "500 Internal Server Error", msg);
  } else {
    ok(c);
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

/* Shows a title logo on `id`'s home-screen page instead of its name in plain text: writes the PNG body to
 * /user/appmeta/<id>/logo.png and points the title's logoImageInfo in the home screen's database at it. The home
 * screen can't read files under /data/homebrew, hence appmeta. Only that one file and that one column change, and
 * only for a title the console already has. SQLite edits the database in place, honouring the home screen's locks. */
static void set_logo(int c, const char *query, const char *head, const char *body, size_t have) {
  static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
  static const char not_png[] = "not a PNG";
  char id[16], dir[64], tmp[96], dest[96], info[128];
  unsigned char sig[8] = {0};
  long long len = content_length(head);
  if (!title_id(query, id)) {
    fail(c, "400 Bad Request", "id must be a title ID");
    return;
  }
  if (len < 8 || len > LOGO_MAX) {
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

  pthread_mutex_lock(&lock);
  const char *err = receive(c, tmp, len, body, have);
  int fd = err ? -1 : open(tmp, O_RDONLY);
  if (!err && (fd < 0 || read(fd, sig, 8) != 8 || memcmp(sig, "\x89PNG\r\n\x1a\n", 8))) err = not_png;
  if (fd >= 0) close(fd);
  if (!err && rename(tmp, dest)) err = strerror(errno);
  if (err) unlink(tmp);

  sqlite3 *db = NULL;
  sqlite3_stmt *st = NULL;
  int changed = 0;
  if (!err) {
    if (sqlite3_open_v2(APP_DB, &db, SQLITE_OPEN_READWRITE, NULL) == SQLITE_OK) {
      sqlite3_busy_timeout(db, 10000);  /* the home screen holds its lock only briefly */
      if (sqlite3_prepare_v2(db, "UPDATE tbl_conceptmetadata SET logoImageInfo = ?1 WHERE localConceptId = "
                                 "(SELECT localConceptId FROM tbl_contentinfo WHERE titleId = ?2)", -1, &st, NULL) == SQLITE_OK) {
        sqlite3_bind_text(st, 1, info, -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(st, 2, id, -1, SQLITE_TRANSIENT);
        if (sqlite3_step(st) == SQLITE_DONE) changed = sqlite3_changes(db);
      }
      if (!changed && sqlite3_errcode(db) != SQLITE_OK && sqlite3_errcode(db) != SQLITE_DONE) err = sqlite3_errmsg(db);
    } else {
      err = "can't open the home screen's database";
    }
  }
  if (err) fail(c, err == not_png ? "400 Bad Request" : "500 Internal Server Error", err);
  else if (!changed) fail(c, "404 Not Found", "the home screen has no entry for this game");
  else ok(c);
  sqlite3_finalize(st);
  sqlite3_close(db);  /* after fail(): err may point into db */
  pthread_mutex_unlock(&lock);
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

  int app = !strncmp(target, RELAY_PATH, sizeof RELAY_PATH - 1);  /* the PS5 app, for the PC */
  if (!authorized(c, head, (post && !strcmp(target, "/quit")) || app)) {
    fail(c, "401 Unauthorized", "wrong key: install the helper again from the PC app");
  } else if (app) {
    if (!from_loopback(c)) {  /* the PC's own API is on the PC: nobody else reaches it through us */
      fail(c, "403 Forbidden", "only the PS5 app on this console can ask for this");
    } else {
      char full[1024];
      snprintf(full, sizeof full, "%s%s%s", target, query ? "?" : "", query ? query : "");
      relay_app(c, method, full, head);
    }
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
  } else if (!has_path && (get || post || !strcmp(method, "PUT"))) {
    fail(c, "400 Bad Request", "p must be an absolute path");
  } else if (get && !strcmp(target, "/ls")) {
    list(c, path);
  } else if (get && !strcmp(target, "/file")) {
    download(c, path);
  } else if (!writable(path)) {
    fail(c, "403 Forbidden", "only the homebrew folders can be changed");
  } else if (post && !strcmp(target, "/copy")) {
    copy_start(c, path, query);
  } else if (post && !strcmp(target, "/install")) {
    install_pkg(c, path, query);
  } else if (!strcmp(method, "PUT") && !strcmp(target, "/file")) {
    upload(c, path, head, end + 4, got - (end + 4 - head));
  } else if (post && !strcmp(target, "/mkdir")) {
    if (mkdir(path, 0777)) fail(c, errno == EEXIST ? "409 Conflict" : "500 Internal Server Error", strerror(errno));
    else ok(c);
  } else if (post && !strcmp(target, "/rename")) {
    if (!param(query, "to", to, sizeof to) || !writable(to)) fail(c, "403 Forbidden", "only the homebrew folders can be changed");
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
  notify("Spela helper " VERSION " is running.");

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
