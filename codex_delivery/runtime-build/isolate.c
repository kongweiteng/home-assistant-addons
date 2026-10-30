#define _GNU_SOURCE
/* Fixed native launcher candidate. Local qualification is mandatory.
 * No mount, host capability, credentials, shell, or variable environment.
 */
#include <errno.h>
#include <dirent.h>
#include <ftw.h>
#include <fcntl.h>
#include <linux/capability.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <linux/landlock.h>
#include <linux/securebits.h>
#include <sched.h>
#include <seccomp.h>
#include <signal.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>
static void require(int condition, const char *code);
#ifdef NATIVE_RESOURCE
#include <pthread.h>
#include <sys/mman.h>
#define RESOURCE_PROFILE "native-resource-v1"
static rlim_t address_budget, thread_budget;
static int cpu_budget;
static unsigned int qualified_threads;
static volatile int threads_release;
static void *qualification_thread(void *unused) {
    (void) unused;
    while (!__atomic_load_n(&threads_release, __ATOMIC_ACQUIRE)) sched_yield();
    return NULL;
}
static void resource_qualification(void) {
    cpu_set_t allowed; CPU_ZERO(&allowed);
    require(sched_getaffinity(0, sizeof(allowed), &allowed) == 0 &&
        CPU_COUNT(&allowed) >= 1 && CPU_COUNT(&allowed) <= cpu_budget, "cpu_affinity_read");
    errno = 0;
    require(sched_setaffinity(0, sizeof(allowed), &allowed) == -1 && errno == EPERM, "cpu_affinity_locked");
    struct rlimit limits;
    require(syscall(SYS_prlimit64, 0, RLIMIT_AS, NULL, &limits) == 0 &&
        limits.rlim_cur == address_budget && limits.rlim_max == address_budget, "address_budget_read");
    errno = 0;
    require(syscall(SYS_prlimit64, 0, RLIMIT_AS, &limits, NULL) == -1 && errno == EPERM, "limits_locked");
    errno = 0;
    require(mmap(NULL, address_budget + 4096, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0) == MAP_FAILED &&
        errno == ENOMEM, "address_budget_enforced");
    pthread_t threads[257]; unsigned int count = 0; int error = 0;
    pthread_attr_t attributes;
    size_t stack_bytes = PTHREAD_STACK_MIN * 2;
    if (stack_bytes < 262144) stack_bytes = 262144;
    require(pthread_attr_init(&attributes) == 0 && pthread_attr_setstacksize(&attributes, stack_bytes) == 0, "thread_attributes");
    while (count <= thread_budget && count < 257) {
        error = pthread_create(&threads[count], &attributes, qualification_thread, NULL);
        if (error) break;
        count++;
    }
    require(count >= 1 && count < thread_budget && error == EAGAIN, "thread_budget_enforced");
    qualified_threads = count;
    __atomic_store_n(&threads_release, 1, __ATOMIC_RELEASE);
    for (unsigned int i = 0; i < count; i++) require(pthread_join(threads[i], NULL) == 0, "thread_join");
    require(pthread_attr_destroy(&attributes) == 0, "thread_attributes_close");
}
#else
#define RESOURCE_PROFILE "cgroup-v1"
#endif

#if !defined(__x86_64__) && !defined(__aarch64__)
#error Unsupported native isolation architecture
#endif
#ifdef READONLY_JAIL
#define PROFILE "readonly-jail-v1"
#else
#define PROFILE "landlock-v1"
#endif

#ifndef LANDLOCK_ACCESS_FS_TRUNCATE
#define LANDLOCK_ACCESS_FS_TRUNCATE (1ULL << 14)
#endif

static void fail(const char *code) {
    dprintf(2, "QEMU_ISOLATION_ERROR=%s:%d\n", code, errno);
    _exit(125);
}
static void require(int condition, const char *code) { if (!condition) fail(code); }
static void write_map(const char *path, const char *text) {
    int fd = open(path, O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
    require(fd >= 0, "map_open");
    size_t length = strlen(text);
    require(write(fd, text, length) == (ssize_t) length, "map_write");
    require(close(fd) == 0, "map_close");
}
static void caps_empty(void) {
    struct __user_cap_header_struct header = {_LINUX_CAPABILITY_VERSION_3, 0};
    struct __user_cap_data_struct data[2] = {{0}, {0}};
    require(syscall(SYS_capget, &header, data) == 0, "caps_read");
    require(!(data[0].effective | data[1].effective | data[0].permitted | data[1].permitted), "outer_caps");
}
static void namespace_chroot(const char *root) {
    uid_t uid = getuid(); gid_t gid = getgid();
    char mapping[96];
    require(uid != 0 && geteuid() == uid && getegid() == gid, "outer_uid");
    caps_empty();
    require(prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) == 1, "outer_nnp");
    int directory = open(root, O_PATH | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    require(directory >= 0, "root_open");
    require(unshare(CLONE_NEWUSER) == 0, "userns");
    write_map("/proc/self/setgroups", "deny\n");
    snprintf(mapping, sizeof(mapping), "0 %lu 1\n", (unsigned long) uid);
    write_map("/proc/self/uid_map", mapping);
    snprintf(mapping, sizeof(mapping), "0 %lu 1\n", (unsigned long) gid);
    write_map("/proc/self/gid_map", mapping);
    require(getuid() == 0 && getgid() == 0, "inner_uid");
    require(fchdir(directory) == 0 && chroot(".") == 0 && chdir("/") == 0, "chroot");
    require(syscall(SYS_close_range, 3U, ~0U, 0) == 0, "close_fds");
    require(prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) == 0, "dumpable");
    require(prctl(PR_SET_SECUREBITS, SECBIT_NOROOT | SECBIT_NOROOT_LOCKED |
        SECBIT_NO_SETUID_FIXUP | SECBIT_NO_SETUID_FIXUP_LOCKED, 0, 0, 0) == 0, "securebits");
    for (int cap = 0; cap < 64; cap++) {
        if (prctl(PR_CAPBSET_DROP, cap, 0, 0, 0) != 0 && errno != EINVAL) fail("bounding_drop");
    }
    struct __user_cap_header_struct header = {_LINUX_CAPABILITY_VERSION_3, 0};
    struct __user_cap_data_struct data[2] = {{0}, {0}};
    require(syscall(SYS_capset, &header, data) == 0, "caps_drop");
    caps_empty();
    require(clearenv() == 0, "environment_clear");
    require(setenv("PATH", "/runtime/bin", 1) == 0 && setenv("LANG", "C", 1) == 0 &&
        setenv("TMPDIR", "/tmp", 1) == 0 && setenv("HOME", "/tmp", 1) == 0 &&
        setenv("QEMU_MODULE_DIR", "/runtime/modules", 1) == 0, "environment_set");
    struct rlimit core = {0, 0};
    require(setrlimit(RLIMIT_CORE, &core) == 0, "core_limit");
}
#ifndef READONLY_JAIL
static void allow_path(int rules, const char *path, uint64_t access) {
    int parent = open(path, O_PATH | O_NOFOLLOW | O_CLOEXEC);
    require(parent >= 0, "landlock_path");
    struct landlock_path_beneath_attr rule = {.allowed_access = access, .parent_fd = parent};
    require(syscall(SYS_landlock_add_rule, rules, LANDLOCK_RULE_PATH_BENEATH, &rule, 0) == 0, "landlock_rule");
    require(close(parent) == 0, "landlock_close");
}
static void filesystem(void) {
    int abi = syscall(SYS_landlock_create_ruleset, NULL, 0, LANDLOCK_CREATE_RULESET_VERSION);
    require(abi >= 3, "landlock_abi");
    uint64_t read = LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR;
    uint64_t all = LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_WRITE_FILE | read |
        LANDLOCK_ACCESS_FS_REMOVE_DIR | LANDLOCK_ACCESS_FS_REMOVE_FILE | LANDLOCK_ACCESS_FS_MAKE_CHAR |
        LANDLOCK_ACCESS_FS_MAKE_DIR | LANDLOCK_ACCESS_FS_MAKE_REG | LANDLOCK_ACCESS_FS_MAKE_SOCK |
        LANDLOCK_ACCESS_FS_MAKE_FIFO | LANDLOCK_ACCESS_FS_MAKE_BLOCK | LANDLOCK_ACCESS_FS_MAKE_SYM |
        LANDLOCK_ACCESS_FS_REFER | LANDLOCK_ACCESS_FS_TRUNCATE;
    struct landlock_ruleset_attr attributes = {.handled_access_fs = all};
    int rules = syscall(SYS_landlock_create_ruleset, &attributes, sizeof(attributes), 0);
    require(rules >= 0, "landlock_create");
    allow_path(rules, "/", LANDLOCK_ACCESS_FS_READ_DIR);
    allow_path(rules, "/runtime", read | LANDLOCK_ACCESS_FS_EXECUTE);
    allow_path(rules, "/input.img", LANDLOCK_ACCESS_FS_READ_FILE);
    allow_path(rules, "/output.img", LANDLOCK_ACCESS_FS_READ_FILE |
        LANDLOCK_ACCESS_FS_WRITE_FILE | LANDLOCK_ACCESS_FS_TRUNCATE);
    allow_path(rules, "/export.bin", LANDLOCK_ACCESS_FS_READ_FILE |
        LANDLOCK_ACCESS_FS_WRITE_FILE | LANDLOCK_ACCESS_FS_TRUNCATE);
    /* No create/remove/rename rights, including /tmp: host file count is fixed. */
    require(prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) == 0, "landlock_nnp");
    require(syscall(SYS_landlock_restrict_self, rules, 0) == 0, "landlock_enforce");
    require(close(rules) == 0, "landlock_finish");
}
#endif
#ifdef READONLY_JAIL
static int readonly_entry(const char *path, const struct stat *info, int type, struct FTW *walk) {
    (void) walk;
    require(info->st_uid == 0 && info->st_gid == 0, "jail_owner");
    mode_t mode = info->st_mode & 07777;
    if (type == FTW_D) require(mode == 0555, "jail_directory_mode");
    else require(type == FTW_F && S_ISREG(info->st_mode) && info->st_nlink == 1 &&
        (mode == 0444 || mode == 0555), "jail_runtime_entry");
    (void) path;
    return 0;
}
static void readonly_jail(void) {
    struct stat info;
    require(lstat("/", &info) == 0 && S_ISDIR(info.st_mode) && (info.st_mode & 07777) == 0555, "jail_root");
    DIR *directory = opendir("/"); require(directory != NULL, "jail_list");
    struct dirent *entry; int count = 0;
    while ((entry = readdir(directory)) != NULL) {
        if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, "..")) continue;
        require(!strcmp(entry->d_name, "runtime") || !strcmp(entry->d_name, "tmp") ||
            !strcmp(entry->d_name, "input.img") || !strcmp(entry->d_name, "output.img") ||
            !strcmp(entry->d_name, "export.bin"), "jail_extra_path");
        count++;
    }
    require(closedir(directory) == 0 && count == 5, "jail_entries");
    require(nftw("/runtime", readonly_entry, 16, FTW_PHYS) == 0, "jail_runtime");
    require(lstat("/tmp", &info) == 0 && S_ISDIR(info.st_mode) && (info.st_mode & 07777) == 0555, "jail_tmp");
    directory = opendir("/tmp"); require(directory != NULL, "jail_tmp_list");
    while ((entry = readdir(directory)) != NULL)
        require(!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, ".."), "jail_tmp_extra");
    require(closedir(directory) == 0, "jail_tmp_close");
    const char *names[] = {"/input.img", "/output.img", "/export.bin"};
    for (int i = 0; i < 3; i++) {
        require(lstat(names[i], &info) == 0 && S_ISREG(info.st_mode) && info.st_nlink == 1 &&
            info.st_uid == 0 && info.st_gid == 0 && (info.st_mode & 07777) == (i == 0 ? 0444 : 0600), "jail_disk_mode");
    }
}
#endif
static void deny(scmp_filter_ctx filter, int syscall_number) {
    if (syscall_number >= 0) require(seccomp_rule_add(filter, SCMP_ACT_ERRNO(EPERM), syscall_number, 0) == 0, "seccomp_rule");
}
static void calls(void) {
    scmp_filter_ctx filter = seccomp_init(SCMP_ACT_ALLOW);
    require(filter != NULL, "seccomp_create");
    int banned[] = {SCMP_SYS(socket), SCMP_SYS(socketpair), SCMP_SYS(connect), SCMP_SYS(bind),
        SCMP_SYS(listen), SCMP_SYS(accept), SCMP_SYS(accept4), SCMP_SYS(io_uring_setup),
        SCMP_SYS(io_uring_enter), SCMP_SYS(io_uring_register), SCMP_SYS(ptrace),
        SCMP_SYS(process_vm_readv), SCMP_SYS(process_vm_writev), SCMP_SYS(pidfd_open),
        SCMP_SYS(pidfd_getfd), SCMP_SYS(pidfd_send_signal), SCMP_SYS(process_madvise),
        SCMP_SYS(kcmp), SCMP_SYS(unshare), SCMP_SYS(setns), SCMP_SYS(mount), SCMP_SYS(umount2),
        SCMP_SYS(fsopen), SCMP_SYS(fsconfig), SCMP_SYS(fsmount), SCMP_SYS(fspick),
        SCMP_SYS(move_mount), SCMP_SYS(open_tree), SCMP_SYS(mount_setattr),
        SCMP_SYS(pivot_root), SCMP_SYS(chroot), SCMP_SYS(open_by_handle_at), SCMP_SYS(name_to_handle_at),
        SCMP_SYS(bpf), SCMP_SYS(perf_event_open), SCMP_SYS(keyctl), SCMP_SYS(add_key), SCMP_SYS(request_key),
        SCMP_SYS(fork), SCMP_SYS(vfork), SCMP_SYS(tkill), SCMP_SYS(shmget), SCMP_SYS(shmat),
        SCMP_SYS(shmctl), SCMP_SYS(msgget), SCMP_SYS(msgsnd), SCMP_SYS(msgrcv), SCMP_SYS(msgctl),
        SCMP_SYS(semget), SCMP_SYS(semop), SCMP_SYS(semctl), SCMP_SYS(semtimedop),
        SCMP_SYS(mq_open), SCMP_SYS(mq_unlink), SCMP_SYS(mq_timedsend), SCMP_SYS(mq_timedreceive),
        SCMP_SYS(mq_notify), SCMP_SYS(mq_getsetattr), SCMP_SYS(fanotify_init), SCMP_SYS(fanotify_mark),
        SCMP_SYS(sched_setscheduler), SCMP_SYS(sched_setparam), SCMP_SYS(sched_setattr),
        SCMP_SYS(sched_setaffinity), SCMP_SYS(setpriority), SCMP_SYS(ioprio_set),
        SCMP_SYS(migrate_pages), SCMP_SYS(move_pages)};
    for (size_t i = 0; i < sizeof(banned) / sizeof(banned[0]); i++) deny(filter, banned[i]);
#ifdef READONLY_JAIL
    int mutations[] = {SCMP_SYS(creat), SCMP_SYS(mkdir), SCMP_SYS(mkdirat), SCMP_SYS(mknod),
        SCMP_SYS(mknodat), SCMP_SYS(link), SCMP_SYS(linkat), SCMP_SYS(symlink), SCMP_SYS(symlinkat),
        SCMP_SYS(rename), SCMP_SYS(renameat), SCMP_SYS(renameat2), SCMP_SYS(unlink), SCMP_SYS(unlinkat),
        SCMP_SYS(rmdir), SCMP_SYS(chmod), SCMP_SYS(fchmod), SCMP_SYS(fchmodat),
        SCMP_SYS(chown), SCMP_SYS(fchown), SCMP_SYS(lchown), SCMP_SYS(fchownat),
        SCMP_SYS(utime), SCMP_SYS(utimes), SCMP_SYS(futimesat), SCMP_SYS(utimensat),
        SCMP_SYS(setxattr), SCMP_SYS(lsetxattr), SCMP_SYS(fsetxattr), SCMP_SYS(removexattr),
        SCMP_SYS(lremovexattr), SCMP_SYS(fremovexattr), SCMP_SYS(openat2)};
    for (size_t i = 0; i < sizeof(mutations) / sizeof(mutations[0]); i++) deny(filter, mutations[i]);
    /* fchmodat2 is native syscall 452 on both explicitly supported arches.
     * Debian 12 libseccomp headers predate this kernel interface. */
    deny(filter, 452);
    int opens[] = {SCMP_SYS(open), SCMP_SYS(openat)};
    for (size_t i = 0; i < sizeof(opens) / sizeof(opens[0]); i++) if (opens[i] >= 0) {
        unsigned int arg = i == 0 ? 1 : 2;
        require(seccomp_rule_add(filter, SCMP_ACT_ERRNO(EPERM), opens[i], 1,
            SCMP_CMP(arg, SCMP_CMP_MASKED_EQ, O_CREAT, O_CREAT)) == 0, "open_create");
        require(seccomp_rule_add(filter, SCMP_ACT_ERRNO(EPERM), opens[i], 1,
            SCMP_CMP(arg, SCMP_CMP_MASKED_EQ, O_TMPFILE, O_TMPFILE)) == 0, "open_tmpfile");
    }
#endif
    require(seccomp_rule_add(filter, SCMP_ACT_ERRNO(ENOSYS), SCMP_SYS(clone3), 0) == 0, "clone3");
    /* KEEP_SIZE can allocate disk without growing st_size/RLIMIT_FSIZE. */
    require(seccomp_rule_add(filter, SCMP_ACT_ERRNO(EOPNOTSUPP), SCMP_SYS(fallocate), 0) == 0, "fallocate");
    require(seccomp_rule_add(filter, SCMP_ACT_ERRNO(ENOTTY), SCMP_SYS(ioctl), 0) == 0, "ioctl");
    int ownership_commands[] = {F_SETOWN, F_SETOWN_EX, F_SETSIG, F_NOTIFY};
    for (size_t i = 0; i < sizeof(ownership_commands) / sizeof(ownership_commands[0]); i++)
        require(seccomp_rule_add(filter, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(fcntl), 1,
            SCMP_A1(SCMP_CMP_EQ, ownership_commands[i])) == 0, "fcntl_owner");
    require(seccomp_rule_add(filter, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(fcntl), 2,
        SCMP_A1(SCMP_CMP_EQ, F_SETFL), SCMP_A2(SCMP_CMP_MASKED_EQ, O_ASYNC, O_ASYNC)) == 0, "fcntl_async");
    require(seccomp_rule_add(filter, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(prctl), 1,
        SCMP_A0(SCMP_CMP_EQ, PR_SET_PDEATHSIG)) == 0, "parent_death_fence");
    uint64_t namespace_flags = CLONE_NEWUSER | CLONE_NEWNS | CLONE_NEWPID | CLONE_NEWNET |
        CLONE_NEWIPC | CLONE_NEWUTS | CLONE_NEWCGROUP | CLONE_PARENT;
    /* Deny either missing required bit, and every namespace flag individually. */
    require(seccomp_rule_add(filter, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(clone), 1,
        SCMP_A0(SCMP_CMP_MASKED_EQ, CLONE_VM, 0)) == 0, "clone_without_vm");
    require(seccomp_rule_add(filter, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(clone), 1,
        SCMP_A0(SCMP_CMP_MASKED_EQ, CLONE_THREAD, 0)) == 0, "clone_without_thread");
    for (uint64_t bit = 1; bit != 0; bit <<= 1) if (namespace_flags & bit)
        require(seccomp_rule_add(filter, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(clone), 1,
            SCMP_A0(SCMP_CMP_MASKED_EQ, bit, bit)) == 0, "clone_namespace");
    uint64_t pid = (uint64_t) getpid();
    int self_only[] = {SCMP_SYS(kill), SCMP_SYS(tgkill), SCMP_SYS(rt_sigqueueinfo), SCMP_SYS(rt_tgsigqueueinfo)};
    for (size_t i = 0; i < sizeof(self_only) / sizeof(self_only[0]); i++)
        require(seccomp_rule_add(filter, SCMP_ACT_ERRNO(EPERM), self_only[i], 1,
            SCMP_A0(SCMP_CMP_NE, pid)) == 0, "signal_parent");
#ifdef NATIVE_RESOURCE
    require(seccomp_rule_add(filter, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(prlimit64), 1,
        SCMP_A0(SCMP_CMP_NE, 0)) == 0, "prlimit_parent");
    require(seccomp_rule_add(filter, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(prlimit64), 1,
        SCMP_A2(SCMP_CMP_NE, 0)) == 0, "prlimit_mutation");
    deny(filter, SCMP_SYS(setrlimit));
#else
    require(seccomp_rule_add(filter, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(prlimit64), 1,
        SCMP_A0(SCMP_CMP_NE, 0)) == 0, "prlimit_parent");
#endif
    require(seccomp_rule_add(filter, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(get_robust_list), 1,
        SCMP_A0(SCMP_CMP_NE, 0)) == 0, "robust_parent");
    require(seccomp_load(filter) == 0, "seccomp_enforce");
    seccomp_release(filter);
#ifdef READONLY_JAIL
    /* Freeze the reviewed syscall universe. New interfaces such as
     * setxattrat/removexattrat/file_setattr cannot bypass the older-name rules. */
#if defined(__x86_64__)
    const uint32_t native_arch = AUDIT_ARCH_X86_64;
#else
    const uint32_t native_arch = AUDIT_ARCH_AARCH64;
#endif
    struct sock_filter instructions[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, native_arch, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
        BPF_JUMP(BPF_JMP | BPF_JGT | BPF_K, 452, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW)
    };
    struct sock_fprog program = {.len = sizeof(instructions) / sizeof(instructions[0]), .filter = instructions};
    require(prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &program) == 0, "syscall_ceiling");
#endif
}
static void self_test(pid_t outside_pid) {
    struct stat info;
    for (int fd = 3; fd < 128; fd++) {
        errno = 0; require(fcntl(fd, F_GETFD) == -1 && errno == EBADF, "fd_leak");
    }
    require(getenv("TEST_CONTROL_SENTINEL") == NULL && getenv("LD_PRELOAD") == NULL, "environment_leak");
    errno = 0; require(stat("/outside-sentinel", &info) == -1 && errno == ENOENT, "outside_stat");
    errno = 0; require(open("/runtime/readonly.txt", O_WRONLY) == -1 && errno == EACCES, "runtime_readonly");
    errno = 0; require(open("/input.img", O_WRONLY) == -1 && errno == EACCES, "inputs_readonly");
    int output = open("/output.img", O_WRONLY | O_CLOEXEC); require(output >= 0, "output_writable"); close(output);
    errno = 0; require(open("/tmp/unbounded-file", O_WRONLY | O_CREAT, 0600) == -1 && (errno == EACCES || errno == EPERM), "tmp_create_denied");
#ifdef READONLY_JAIL
    errno = 0; require(fchmodat(AT_FDCWD, "/runtime/readonly.txt", 0444, 0) == -1 && errno == EPERM, "chmodat_denied");
    errno = 0; require(syscall(452, AT_FDCWD, "/runtime/readonly.txt", 0444, 0) == -1 && errno == EPERM, "chmodat2_denied");
    errno = 0; require(syscall(463, AT_FDCWD, NULL, 0, NULL) == -1 && errno == EPERM, "new_syscall_denied");
#endif
    errno = 0; require(socket(AF_INET, SOCK_STREAM, 0) == -1 && errno == EPERM, "network_denied");
    errno = 0; require(socket(AF_UNIX, SOCK_STREAM, 0) == -1 && errno == EPERM, "unix_socket_denied");
    errno = 0; require(syscall(SYS_connect, -1, NULL, 0) == -1 && errno == EPERM, "connect_denied");
    errno = 0; require(syscall(SYS_io_uring_setup, 0, NULL) == -1 && errno == EPERM, "uring_denied");
    errno = 0; require(kill(outside_pid, 0) == -1 && errno == EPERM, "parent_signal_denied");
    errno = 0; require(syscall(SYS_ptrace, 0, outside_pid, NULL, NULL) == -1 && errno == EPERM, "ptrace_denied");
    errno = 0; require(syscall(SYS_process_vm_readv, outside_pid, NULL, 0, NULL, 0, 0) == -1 && errno == EPERM, "process_read_denied");
    errno = 0; require(syscall(SYS_pidfd_open, outside_pid, 0) == -1 && errno == EPERM, "pidfd_denied");
    struct rlimit limit;
    errno = 0; require(syscall(SYS_prlimit64, outside_pid, RLIMIT_NOFILE, NULL, &limit) == -1 && errno == EPERM, "prlimit_denied");
    errno = 0; require(syscall(SYS_unshare, CLONE_NEWUSER) == -1 && errno == EPERM, "new_namespace_denied");
    caps_empty();
#ifdef NATIVE_RESOURCE
    resource_qualification();
    printf("{\"resource_profile\":\"native-resource-v1\",\"expected_parent_bound\":true,\"address_bytes\":%llu,\"thread_limit\":%llu,\"created_threads\":%u,\"logical_cpus\":%d,\"qualified\":true}\n",
        (unsigned long long) address_budget, (unsigned long long) thread_budget, qualified_threads, cpu_budget);
#else
    printf("{\"version\":1,\"chroot\":true,\"landlock\":%s,\"isolation_profile\":\"%s\",\"seccomp\":true,\"caps\":0,\"network_denied\":true,\"parent_signal_denied\":true,\"outside_stat_denied\":true,\"inputs_readonly\":true}\n",
#ifdef READONLY_JAIL
        "false",
#else
        "true",
#endif
        PROFILE);
#endif
}
int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--profile") == 0) { puts(PROFILE); return 0; }
    if (argc == 2 && strcmp(argv[1], "--resource-profile") == 0) { puts(RESOURCE_PROFILE); return 0; }
    pid_t parent = getppid();
    require(parent > 0 && prctl(PR_SET_PDEATHSIG, SIGKILL, 0, 0, 0) == 0, "parent_death");
    require(argc >= 4 && argv[1][0] == '/', "arguments");
    int test = strcmp(argv[2], "--self-test") == 0;
    pid_t outside = test ? (pid_t) strtol(argv[3], NULL, 10) : 0;
    int command_index = 5;
    if (test) require(outside > 0 && outside != getpid(), "test_pid");
#ifdef NATIVE_RESOURCE
    else require(argc >= 10 && strcmp(argv[2], "--exec-resource") == 0 && argv[9][0] == '/', "resource_command");
    require(test ? argc == 7 : argc >= 10, "resource_arguments");
    char *end = NULL; int start = test ? 4 : 5;
    long expected_parent = outside;
    if (!test) {
        errno = 0; expected_parent = strtol(argv[8], &end, 10);
        require(errno == 0 && end && *end == 0 && expected_parent > 0 && expected_parent <= INT32_MAX, "parent_argument");
    }
    /* A dead creator must not be replaced by PID 1 in the fork/exec window. */
    require(parent == (pid_t) expected_parent && getppid() == (pid_t) expected_parent, "parent_identity");
    require(prctl(PR_SET_PDEATHSIG, SIGKILL, 0, 0, 0) == 0 &&
        getppid() == (pid_t) expected_parent, "parent_changed");
    errno = 0; address_budget = strtoull(argv[start], &end, 10);
    require(errno == 0 && end && *end == 0 && address_budget >= (128ULL << 20) && address_budget <= (8ULL << 30), "address_budget");
    errno = 0; thread_budget = strtoull(argv[start+1], &end, 10);
    require(errno == 0 && end && *end == 0 && thread_budget >= 4 && thread_budget <= 256, "thread_budget");
    errno = 0; cpu_budget = strtol(argv[start+2], &end, 10);
    require(errno == 0 && end && *end == 0 && cpu_budget >= 1 && cpu_budget <= 2, "cpu_budget");
    command_index = 9;
#else
    else require(argc >= 6 && strcmp(argv[2], "--exec") == 0 && argv[5][0] == '/', "command");
#endif
    unsigned long long file_bytes = 1048576, cpu_seconds = 2;
    if (!test) {
        char *end = NULL;
        errno = 0; file_bytes = strtoull(argv[3], &end, 10);
        require(errno == 0 && end && *end == 0 && file_bytes >= 4096 && file_bytes <= (1ULL << 37), "file_budget");
        errno = 0; cpu_seconds = strtoull(argv[4], &end, 10);
        require(errno == 0 && end && *end == 0 && cpu_seconds >= 1 && cpu_seconds <= 14400, "cpu_budget");
    }
    namespace_chroot(argv[1]);
    require(prctl(PR_SET_PDEATHSIG, SIGKILL, 0, 0, 0) == 0 && getppid() == parent, "parent_changed");
#ifdef READONLY_JAIL
    readonly_jail();
    if (!test) {
        struct stat output, descriptor;
        require(lstat("/export.bin", &output) == 0 && fstat(STDOUT_FILENO, &descriptor) == 0 &&
            descriptor.st_dev == output.st_dev && descriptor.st_ino == output.st_ino &&
            (fcntl(STDOUT_FILENO, F_GETFL) & O_ACCMODE) == O_WRONLY, "export_stdout_identity");
    }
#else
    filesystem();
#endif
    struct rlimit file_limit = {file_bytes, file_bytes}, cpu_limit = {cpu_seconds, cpu_seconds};
    struct rlimit descriptors = {128, 128};
    require(setrlimit(RLIMIT_FSIZE, &file_limit) == 0 && setrlimit(RLIMIT_CPU, &cpu_limit) == 0 &&
        setrlimit(RLIMIT_NOFILE, &descriptors) == 0, "resource_limits");
#ifdef NATIVE_RESOURCE
    cpu_set_t inherited, selected; CPU_ZERO(&inherited); CPU_ZERO(&selected);
    require(sched_getaffinity(0, sizeof(inherited), &inherited) == 0, "cpu_affinity_get");
    int chosen = 0;
    for (int i = 0; i < CPU_SETSIZE && chosen < cpu_budget; i++) if (CPU_ISSET(i, &inherited)) {
        CPU_SET(i, &selected); chosen++;
    }
    require(chosen >= 1 && sched_setaffinity(0, sizeof(selected), &selected) == 0, "cpu_affinity_set");
    cpu_budget = chosen;
    struct rlimit address = {address_budget, address_budget}, threads = {thread_budget, thread_budget};
    require(setrlimit(RLIMIT_AS, &address) == 0 && setrlimit(RLIMIT_NPROC, &threads) == 0, "native_resource_limits");
#endif
    calls();
    if (test) { self_test(outside); return 0; }
    execv(argv[command_index], &argv[command_index]); fail("exec");
}
