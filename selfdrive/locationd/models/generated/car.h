#pragma once
#include "rednose/helpers/ekf.h"
extern "C" {
void car_update_25(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void car_update_24(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void car_update_30(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void car_update_26(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void car_update_27(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void car_update_29(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void car_update_28(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void car_update_31(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void car_err_fun(double *nom_x, double *delta_x, double *out_7831386489480772313);
void car_inv_err_fun(double *nom_x, double *true_x, double *out_5445972392415627666);
void car_H_mod_fun(double *state, double *out_1803746137650775980);
void car_f_fun(double *state, double dt, double *out_6451811560616817693);
void car_F_fun(double *state, double dt, double *out_6181843580237958182);
void car_h_25(double *state, double *unused, double *out_5273530629018735411);
void car_H_25(double *state, double *unused, double *out_7389442088261461257);
void car_h_24(double *state, double *unused, double *out_5304902909850479935);
void car_H_24(double *state, double *unused, double *out_8884652386442590793);
void car_h_30(double *state, double *unused, double *out_1636081100764713061);
void car_H_30(double *state, double *unused, double *out_472751746769844502);
void car_h_26(double *state, double *unused, double *out_7570122019416607006);
void car_H_26(double *state, double *unused, double *out_7315798666574034135);
void car_h_27(double *state, double *unused, double *out_6963978463754416371);
void car_H_27(double *state, double *unused, double *out_2647515058570269413);
void car_h_29(double *state, double *unused, double *out_3589826115070611886);
void car_H_29(double *state, double *unused, double *out_4360877785439820446);
void car_h_28(double *state, double *unused, double *out_8445979558510802560);
void car_H_28(double *state, double *unused, double *out_9003467271200200596);
void car_h_31(double *state, double *unused, double *out_6585385518214521042);
void car_H_31(double *state, double *unused, double *out_7358796126384500829);
void car_predict(double *in_x, double *in_P, double *in_Q, double dt);
void car_set_mass(double x);
void car_set_rotational_inertia(double x);
void car_set_center_to_front(double x);
void car_set_center_to_rear(double x);
void car_set_stiffness_front(double x);
void car_set_stiffness_rear(double x);
}