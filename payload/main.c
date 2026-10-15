/* PS5 Local Storefront helper: a small HTTP server the PC app loads through the console's ELF loader.
 *   GET  /info                 {"version", "firmware", "model", "mounts": [{"mount", "total", "free"}]}
 *   GET  /ls?p=<dir>           [{"name", "dir", "size"}]; 404 when the folder is missing
 *   GET  /file?p=<file>        the file's bytes
 *   PUT  /file?p=<file>        writes the request body (needs Content-Length) to the file
 *   POST /mkdir?p=<dir>  /rename?p=<from>&to=<to>  /rm?p=<file or empty folder>
 *   POST /quit                 stops the helper, so a newer copy can take the port (a new copy sends it to an old one itself)
 * Paths are percent-encoded. Anyone on the network can reach this port, so writes are refused outside the
 * homebrew folders (writable()). One thread per connection: /info still answers during a long upload.
 * Build with ./build.ps1; VERSION comes from backend/app/__init__.py. */
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/mount.h>
#include <sys/param.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/time.h>

#define PORT 7735
#define BLOCK (1 << 20)

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

/* The PC app finds the bundled helper's version by searching the .elf for this string. */
__attribute__((used)) static const char VERSION_MARKER[] = "storefront-helper-version:" VERSION;

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

/* Decodes query parameter `key` into `out`; 1 for an absolute path that fit, else 0. */
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
    return out[0] == '/';
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

/* `body`/`have`: the part of the body that arrived with the headers. */
static void upload(int c, const char *path, const char *head, const char *body, size_t have) {
  const char *cl = strstr(head, "\r\nContent-Length:");  /* the PC app is the only client; it sends this spelling */
  if (!cl) {
    fail(c, "411 Length Required", "Content-Length missing");
    return;
  }
  unsigned long long left = strtoull(cl + 17, NULL, 10);
  int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0666);
  if (fd < 0) {
    fail(c, "500 Internal Server Error", strerror(errno));
    return;
  }
  char *chunk = malloc(BLOCK);
  const char *err = chunk ? NULL : "out of memory";
  if (have > left) have = left;
  for (const char *p = body; !err && left;) {
    if (!have) {
      ssize_t r = recv(c, chunk, left < BLOCK ? left : BLOCK, 0);
      if (r <= 0) err = "upload cut off";
      p = chunk;
      have = r > 0 ? r : 0;
    }
    if (have && write(fd, p, have) != (ssize_t)have) err = strerror(errno);
    left -= have;
    have = 0;
  }
  free(chunk);
  if (close(fd) && !err) err = strerror(errno);
  if (err) fail(c, "500 Internal Server Error", err);
  else ok(c);
}

static int is_dir(const char *path) {
  struct stat st;
  return !lstat(path, &st) && S_ISDIR(st.st_mode);
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
  int get = !strcmp(method, "GET"), post = !strcmp(method, "POST"), has_path = query && param(query, "p", path, sizeof path);

  if (get && !strcmp(target, "/info")) {
    buf_t b = {0};
    info(&b);
    reply(c, "200 OK", &b);
    free(b.s);
  } else if (post && !strcmp(target, "/quit")) {
    ok(c);
    close(c);
    exit(0);
  } else if (!has_path && (get || post || !strcmp(method, "PUT"))) {
    fail(c, "400 Bad Request", "p must be an absolute path");
  } else if (get && !strcmp(target, "/ls")) {
    list(c, path);
  } else if (get && !strcmp(target, "/file")) {
    download(c, path);
  } else if (!writable(path)) {
    fail(c, "403 Forbidden", "only the homebrew folders can be changed");
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
  syscall(SYS_thr_set_name, -1, "storefront.elf");
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
      notify("PS5 Local Storefront helper couldn't start: port 7735 is in use.");
      return 1;
    }
    sleep(1);
  }
  listen(srv, 16);
  notify("PS5 Local Storefront helper " VERSION " is running.");

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
