#!/usr/bin/perl
# SigmaFi - peer-to-peer loans on Ergo (SigmaFi V2 bonds) from the Ergo wallet.
# A subdirectory app of SigmaSwap:  https://<host>/cgi-bin/sigmaswap/sigmafi/
#
# The screens, signing links, status and notification code live in ../sigmalithos/api.pl, which
# serves both apps. This file only says which of the two is being asked for and runs it.

use strict;
use warnings;
use FindBin;

$ENV{SIGMA_APP} = 'sigmafi';
my $main = "$FindBin::Bin/../sigmalithos/api.pl";
do $main;
if ($@ || !-r $main) {    # the shared script is missing or does not compile: nothing was printed yet
    warn "sigmafi: could not run $main: " . ($@ || $!) . "\n";
    print "Content-Type: application/json; charset=utf-8\r\n\r\n", '{"success":0,"error":"SigmaFi is unavailable right now."}';
}
