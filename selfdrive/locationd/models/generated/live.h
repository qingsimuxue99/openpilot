#pragma once
#include "rednose/helpers/ekf.h"
extern "C" {
void live_update_4(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_update_9(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_update_10(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_update_12(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_update_35(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_update_32(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_update_13(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_update_14(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_update_33(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_H(double *in_vec, double *out_3721165653252102127);
void live_err_fun(double *nom_x, double *delta_x, double *out_6179670739110315318);
void live_inv_err_fun(double *nom_x, double *true_x, double *out_21498504400852333);
void live_H_mod_fun(double *state, double *out_3301532070449651342);
void live_f_fun(double *state, double dt, double *out_8261986219538522382);
void live_F_fun(double *state, double dt, double *out_7380163789266844535);
void live_h_4(double *state, double *unused, double *out_1557454856354861510);
void live_H_4(double *state, double *unused, double *out_1584079418805807703);
void live_h_9(double *state, double *unused, double *out_7569915822739771471);
void live_H_9(double *state, double *unused, double *out_8871298354070255173);
void live_h_10(double *state, double *unused, double *out_2754158243071728056);
void live_H_10(double *state, double *unused, double *out_2731600112228303732);
void live_h_12(double *state, double *unused, double *out_6867966856523997111);
void live_H_12(double *state, double *unused, double *out_2205178443853401370);
void live_h_35(double *state, double *unused, double *out_4578924968296625862);
void live_H_35(double *state, double *unused, double *out_4950741476178415079);
void live_h_32(double *state, double *unused, double *out_5516138127025749853);
void live_H_32(double *state, double *unused, double *out_2902128968909120983);
void live_h_13(double *state, double *unused, double *out_2572469827316083502);
void live_H_13(double *state, double *unused, double *out_1920429525604694117);
void live_h_14(double *state, double *unused, double *out_7569915822739771471);
void live_H_14(double *state, double *unused, double *out_8871298354070255173);
void live_h_33(double *state, double *unused, double *out_386222804196762756);
void live_H_33(double *state, double *unused, double *out_8101298480817272683);
void live_predict(double *in_x, double *in_P, double *in_Q, double dt);
}