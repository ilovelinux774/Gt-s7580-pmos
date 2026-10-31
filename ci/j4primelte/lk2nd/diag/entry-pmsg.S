/*
 * J415F experimental AArch64 wrapper-stage marker, not proof Linux started.
 * Called with caches/MMU disabled under the ARM64 boot protocol. A short,
 * vendor-format ring is committed to the pmsg quarter at 0x860c0000; this
 * replaces old volatile pmsg data. The diagnostic DTB reserves this quarter
 * separately from mainline ramoops. Recovery compatibility and reset retention
 * remain unverified. Do not use outside the J415F Rev06 diagnostic experiment.
 *
 * Preserve x0-x8, x15-x30, SP and NZCV. x9-x14 are boot-protocol scratch.
 */
.section .text
.global j4_diag_aarch64_entry
.extern j4_original_image
.type j4_diag_aarch64_entry, %function
j4_diag_aarch64_entry:
    mrs     x13, nzcv
    movz    x9, #0x860c, lsl #16
    str     wzr, [x9]
    dsb     sy
    adr     x10, .Lmessage
    add     x11, x9, #12
    mov     w12, #(.Lmessage_end - .Lmessage)
.Lcopy:
    ldrb    w14, [x10], #1
    strb    w14, [x11], #1
    sub     w12, w12, #1
    cbnz    w12, .Lcopy
    mov     w12, #(.Lmessage_end - .Lmessage)
    str     w12, [x9, #4]
    str     w12, [x9, #8]
    dsb     sy
    movz    w14, #0x4244
    movk    w14, #0x4347, lsl #16
    str     w14, [x9]
    dsb     sy
    msr     nzcv, x13
    b       j4_original_image
.Lmessage:
    .ascii  "J4DIAG-HLOS-1 B: AArch64 wrapper reached; Linux unproven\n"
.Lmessage_end:
.balign 4
.size j4_diag_aarch64_entry, . - j4_diag_aarch64_entry
