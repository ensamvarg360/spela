/* Host check: the same e2a calls as pack_thread() in payload/main.c, on a Linux folder. */
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include "ampr_pack.h"
#include "verify.h"

int activity_workers(int requested) { return requested; }
void activity_throttle(uint64_t bytes) { (void)bytes; }
static atomic_int cancel_flag;

static void progress(void *ctx, uint64_t done, const char *msg) { (void)ctx; (void)done; (void)msg; }

int main(int argc, char **argv) {
  const char *game = argv[1], *work = argv[2], *sprx = argv[3];
  char state[1100], fakelib[1100], dst[1200], why[256], index[1024], note[300];
  e2a_list l = {0};
  e2a_game g = {0};
  e2a_pack_stats st = {0};
  e2a_verify_stats vs = {0};
  e2a_pack_opts o = {0};
  uint64_t verified[16] = {0};
  struct stat s;
  printf("ampr compat %d, read_game %d\n", e2a_ampr_compat(game, why, sizeof why), e2a_read_game(game, &g));
  if (e2a_scan_tree(game, &l, true)) return 2;
  snprintf(state, sizeof state, "%s/.e2a-state", work);
  e2a_mkdir_p(state);
  e2a_blind_tune(&l);
  snprintf(fakelib, sizeof fakelib, "%s/fakelib", work);
  e2a_mkdir_p(fakelib);
  snprintf(dst, sizeof dst, "%s/libSceAmpr.sprx", fakelib);
  if (e2a_copy_file(sprx, dst, time(NULL)) || stat(dst, &s)) return 3;
  e2a_list_add(&l, "fakelib/libSceAmpr.sprx", s.st_size, s.st_mtime, true);
  e2a_list_sort(&l);
  e2a_plan_budget(&l, E2A_BLOCK_BUDGET, note, sizeof note);
  snprintf(index, sizeof index, "%s/ampr_emu.index", work);
  if (e2a_write_index(index, &l)) return 4;
  o.src_root = game; o.out_dir = work; o.state_dir = state; o.level = 9; o.workers = 6; o.dedup = true;
  o.max_pack_size = E2A_MAX_PACK; o.cancel = &cancel_flag; o.progress = progress; o.verified_bits = verified;
  if (e2a_pack(&l, &o, &st) != E2A_PACK_OK) return 5;
  if (e2a_copy_loose(&l, &o) != E2A_PACK_OK) return 6;
  if (e2a_verify(work, &o, &vs) != E2A_PACK_OK) { printf("verify: %s\n", vs.first_error); return 7; }
  e2a_rm_rf(state);
  printf("title %s, packed %llu files, loose %llu, chunks %llu (lz4 %llu)\n", g.title_id,
         (unsigned long long)st.files_packed, (unsigned long long)st.files_loose, (unsigned long long)st.chunks,
         (unsigned long long)st.chunks_lz4);
  return 0;
}
