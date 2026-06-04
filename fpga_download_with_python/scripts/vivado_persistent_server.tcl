# Persistent Vivado Lab worker. Keep messages ASCII for robust client parsing.

if {$argc < 1} {
    puts "ERROR: Missing workspace path."
    exit 1
}

set root_dir [file normalize [lindex $argv 0]]
set server_dir [file join $root_dir ".vivado_server"]
file mkdir $server_dir

set ready_file [file join $server_dir "ready.txt"]
set stop_file [file join $server_dir "stop.txt"]
set server_vivado_log [file join $server_dir "server_vivado.log"]

proc write_text_file {path text} {
    set fp [open $path w]
    puts $fp $text
    close $fp
}

proc append_log {log_file text} {
    set fp [open $log_file a]
    puts $fp $text
    flush $fp
    close $fp
}

proc sync_server_log {source_log job_log start_offset} {
    if {![file exists $source_log]} {
        return [list $start_offset ""]
    }

    set fp [open $source_log r]
    seek $fp $start_offset
    set content [read $fp]
    set new_offset [tell $fp]
    close $fp

    if {$content ne ""} {
        set out [open $job_log a]
        puts -nonewline $out $content
        close $out
    }

    return [list $new_offset $content]
}

proc read_request {request_file} {
    set fp [open $request_file r]
    set lines [split [read $fp] "\n"]
    close $fp

    array set req {}
    foreach line $lines {
        set line [string trim $line]
        if {$line eq ""} {
            continue
        }
        set pos [string first "=" $line]
        if {$pos < 0} {
            continue
        }
        set key [string range $line 0 [expr {$pos - 1}]]
        set value [string range $line [expr {$pos + 1}] end]
        set req($key) $value
    }
    return [array get req]
}

proc get_hw_devices_quiet {pattern} {
    if {[catch {get_hw_devices -quiet $pattern} hw_devices]} {
        return {}
    }
    return $hw_devices
}

proc get_cfgmem_parts_quiet {pattern} {
    if {[catch {get_cfgmem_parts -quiet $pattern} cfgmem_parts]} {
        return {}
    }
    return $cfgmem_parts
}

proc unique_list {items} {
    set unique {}
    foreach item $items {
        if {[lsearch -exact $unique $item] < 0} {
            lappend unique $item
        }
    }
    return $unique
}

proc resolve_hw_device {fpga_device job_log} {
    set hw_devices [get_hw_devices_quiet $fpga_device]

    if {[llength $hw_devices] == 0 && [string first "_" $fpga_device] < 0} {
        set hw_devices [get_hw_devices_quiet "${fpga_device}_*"]
    }

    if {[llength $hw_devices] == 0} {
        set hw_devices [get_hw_devices_quiet "${fpga_device}*"]
    }

    if {[llength $hw_devices] == 0} {
        set available [get_hw_devices_quiet "*"]
        if {[llength $available] > 0} {
            append_log $job_log "Available FPGA devices: $available"
        }
        return ""
    }

    if {[llength $hw_devices] > 1} {
        append_log $job_log "Matched multiple FPGA devices for $fpga_device: $hw_devices"
        append_log $job_log "Using FPGA device: [lindex $hw_devices 0]"
    }
    return [lindex $hw_devices 0]
}

proc resolve_cfgmem_part {flash_part job_log} {
    set cfgmem_parts [get_cfgmem_parts_quiet $flash_part]

    if {[llength $cfgmem_parts] == 0 && [string first "-" $flash_part] < 0} {
        set cfgmem_parts [get_cfgmem_parts_quiet "${flash_part}-*"]
    }

    if {[llength $cfgmem_parts] == 0} {
        set cfgmem_parts [get_cfgmem_parts_quiet "${flash_part}*"]
    }

    if {[llength $cfgmem_parts] == 0} {
        error "Flash part $flash_part was not found in Vivado cfgmem parts."
    }

    set cfgmem_parts [unique_list $cfgmem_parts]

    if {[llength $cfgmem_parts] == 1} {
        return [lindex $cfgmem_parts 0]
    }

    set preferred {}
    foreach part $cfgmem_parts {
        if {[string match "*-spi-x1_x2_x4" $part]} {
            lappend preferred $part
        }
    }
    set preferred [unique_list $preferred]

    if {[llength $preferred] == 1} {
        append_log $job_log "Matched multiple Flash parts for $flash_part: $cfgmem_parts"
        append_log $job_log "Using preferred Flash part: [lindex $preferred 0]"
        return [lindex $preferred 0]
    }

    append_log $job_log "Matched multiple Flash parts for $flash_part: $cfgmem_parts"
    append_log $job_log "FPGA_ERROR_AMBIGUOUS_FLASH_PART"
    foreach part $cfgmem_parts {
        append_log $job_log "FLASH_PART_CANDIDATE: $part"
    }
    error "Flash part $flash_part is ambiguous. Please choose one of the listed Vivado cfgmem parts."
}

proc run_flash_job {bin_file success_flag fpga_device flash_part job_log server_vivado_log} {
    if {[file exists $success_flag]} {
        file delete -force $success_flag
    }

    append_log $job_log "Programming file: $bin_file"
    append_log $job_log "FPGA device: $fpga_device"
    append_log $job_log "Flash part: $flash_part"

    if {![file exists $bin_file]} {
        append_log $job_log "ERROR: Bin file does not exist: $bin_file"
        return 1
    }

    set log_offset 0
    if {[file exists $server_vivado_log]} {
        set log_offset [file size $server_vivado_log]
    }

    if {[catch {open_hw_manager} err]} {
        lassign [sync_server_log $server_vivado_log $job_log $log_offset] log_offset ignored
        append_log $job_log "ERROR: $err"
        return 1
    }

    if {[catch {connect_hw_server -url localhost:3121 -allow_non_jtag} err]} {
        lassign [sync_server_log $server_vivado_log $job_log $log_offset] log_offset ignored
        append_log $job_log "ERROR: $err"
        catch {close_hw_manager}
        return 1
    }

    if {[catch {get_hw_targets */xilinx_tcf/*} hw_targets] || [llength $hw_targets] == 0} {
        lassign [sync_server_log $server_vivado_log $job_log $log_offset] log_offset ignored
        append_log $job_log "FPGA_ERROR_NO_DOWNLOADER"
        append_log $job_log "ERROR: No JTAG downloader target was detected."
        catch {close_hw_manager}
        return 2
    }

    if {[catch {
        current_hw_target [lindex $hw_targets 0]
        open_hw_target
    } err]} {
        lassign [sync_server_log $server_vivado_log $job_log $log_offset] log_offset ignored
        append_log $job_log "FPGA_ERROR_TARGET_NOT_OPEN"
        append_log $job_log "ERROR: $err"
        catch {close_hw_manager}
        return 4
    }

    set hw_device [resolve_hw_device $fpga_device $job_log]
    if {$hw_device eq ""} {
        lassign [sync_server_log $server_vivado_log $job_log $log_offset] log_offset ignored
        append_log $job_log "FPGA_ERROR_NO_FPGA_DEVICE"
        append_log $job_log "ERROR: JTAG downloader was detected, but target FPGA $fpga_device was not found."
        catch {close_hw_manager}
        return 3
    }

    if {[catch {
        current_hw_device $hw_device
        refresh_hw_device -update_hw_probes false $hw_device

        set cfgmem_part [resolve_cfgmem_part $flash_part $job_log]
        create_hw_cfgmem -hw_device $hw_device -mem_dev $cfgmem_part
        set hw_cfgmem [get_property PROGRAM.HW_CFGMEM $hw_device]

        set_property PROGRAM.BLANK_CHECK 0 $hw_cfgmem
        set_property PROGRAM.ERASE 1 $hw_cfgmem
        set_property PROGRAM.CFG_PROGRAM 1 $hw_cfgmem
        set_property PROGRAM.VERIFY 1 $hw_cfgmem
        set_property PROGRAM.CHECKSUM 0 $hw_cfgmem
        set_property PROGRAM.ADDRESS_RANGE {use_file} $hw_cfgmem
        set_property PROGRAM.FILES [list $bin_file] $hw_cfgmem

        append_log $job_log "STEP 1: Load Flash bridge into FPGA..."
        create_hw_bitstream -hw_device $hw_device [get_property PROGRAM.HW_CFGMEM_BITFILE $hw_device]
        if {[catch {program_hw_devices $hw_device} err]} {
            lassign [sync_server_log $server_vivado_log $job_log $log_offset] log_offset ignored
            append_log $job_log "ERROR: $err"
            error $err
        }
        lassign [sync_server_log $server_vivado_log $job_log $log_offset] log_offset ignored
        refresh_hw_device $hw_device

        append_log $job_log "STEP 2: Program Flash memory..."
        append_log $job_log "TCL_PROGRESS_FLASH_START"
        if {[catch {program_hw_cfgmem -hw_cfgmem $hw_cfgmem} err]} {
            lassign [sync_server_log $server_vivado_log $job_log $log_offset] log_offset ignored
            append_log $job_log "ERROR: $err"
            error $err
        }
        lassign [sync_server_log $server_vivado_log $job_log $log_offset] log_offset ignored
        append_log $job_log "TCL_PROGRESS_FLASH_DONE"

        append_log $job_log "STEP 3: Boot FPGA from Flash..."
        append_log $job_log "TCL_PROGRESS_BOOT_START"
        if {[catch {boot_hw_device $hw_device} err]} {
            lassign [sync_server_log $server_vivado_log $job_log $log_offset] log_offset ignored
            append_log $job_log "ERROR: $err"
            error $err
        }
        lassign [sync_server_log $server_vivado_log $job_log $log_offset] log_offset ignored
        append_log $job_log "TCL_PROGRESS_BOOT_DONE"
    } err]} {
        append_log $job_log "ERROR: $err"
        catch {close_hw_manager}
        return 1
    }

    catch {close_hw_manager}

    set fp [open $success_flag w]
    puts $fp "SUCCESS"
    close $fp

    append_log $job_log "FLASH_PROGRAM_SUCCESS"
    return 0
}

proc process_request {request_file server_dir server_vivado_log} {
    array set req [read_request $request_file]
    if {![info exists req(job_id)] || ![info exists req(bin_file)] || ![info exists req(success_flag)] || ![info exists req(fpga_device)] || ![info exists req(flash_part)]} {
        file delete -force $request_file
        return
    }

    set job_id $req(job_id)
    set job_log [file join $server_dir "$job_id.log"]
    set done_file [file join $server_dir "$job_id.done"]

    file delete -force $job_log
    file delete -force $done_file
    file delete -force $request_file

    append_log $job_log "VIVADO_PERSISTENT_JOB_START"
    set code [run_flash_job $req(bin_file) $req(success_flag) $req(fpga_device) $req(flash_part) $job_log $server_vivado_log]
    append_log $job_log "VIVADO_PERSISTENT_JOB_DONE $code"
    write_text_file $done_file $code
}

set script_mtime [file mtime [info script]]
write_text_file $ready_file "pid=[pid]\nscript_mtime=$script_mtime"
puts "VIVADO_PERSISTENT_SERVER_READY"
flush stdout

while {1} {
    if {[file exists $stop_file]} {
        file delete -force $stop_file
        break
    }

    set requests [glob -nocomplain [file join $server_dir "*.req"]]
    foreach request_file $requests {
        process_request $request_file $server_dir $server_vivado_log
    }

    after 200
}

exit 0
